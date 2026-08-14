"""DART OpenAPI 전용 httpx.AsyncClient 싱글톤 + 동시 요청 제한 + 재시도.

DART는 분당 1,000건 / 일 20,000건 제한이 있다. 초기 Semaphore=10으로 보수적으로
시작해서 운영하며 조정한다.

응답 형식:
- JSON: {"status": "000", "message": "정상", ...} — status가 "000"이 아니면 에러
- XML: corpCode.xml(zip), document.xml — 호출처에서 직접 파싱
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
from typing import Any

import httpx

from dartlens._safe import DartApiError, require_api_key

# 보안: httpx/httpcore의 INFO 로그는 요청 URL을 그대로 찍는다.
# DART는 crtfc_key를 query string으로 받기 때문에 그 로그가 stderr로 빠지면
# Claude Desktop / 시스템 로그에 API 키가 평문으로 남는다.
# WARNING으로 잠가서 차단.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


_BASE_URL = "https://opendart.fss.or.kr/api"
_TIMEOUT = 15.0  # 공시 본문/재무제표는 응답이 클 수 있어 stocklens(8s)보다 여유

_HEADERS = {
    "User-Agent": "dartlens-mcp (+https://github.com/Johnhyeon/dartlens-mcp)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

# DART rate limit 보수 시작값 (분당 1,000건 한도 → 동시 10개면 충분)
_MAX_CONCURRENT = 10
_semaphore: asyncio.Semaphore | None = None
_client: httpx.AsyncClient | None = None

_ssl_ctx: "ssl.SSLContext | bool | None" = None


def _verify() -> "ssl.SSLContext | bool":
    """TLS 검증에 OS 인증서 저장소를 쓴다(브라우저·curl과 같은 기준).

    httpx 기본값은 certifi 번들만 믿는다. 백신이나 회사망 프록시가 TLS를 가로채
    자기 루트로 재서명하면, 그 루트는 Windows 저장소에는 있어도 certifi 에는 없어
    전부 이렇게 죽는다:

        ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local
        issuer certificate

    실제 문의(2026-08-13, 뉴질랜드 사용자)에서 확인된 원인이다. StockLens
    _http.py 와 같은 처리 — 한쪽만 고치면 같은 PC에서 시세는 되는데 공시만 안 되는
    상태가 된다.

    truststore 를 못 쓰면 기존 동작(certifi)으로 조용히 돌아간다. 검증을 끄는
    선택지는 두지 않는다.
    """
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import truststore

            _ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _ssl_ctx = True
    return _ssl_ctx


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=_TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
            verify=_verify(),
            limits=httpx.Limits(
                max_keepalive_connections=15,
                max_connections=25,
                keepalive_expiry=60.0,
            ),
        )
    return _client


async def _request(
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = 2,
    timeout: "httpx.Timeout | float | None" = None,
) -> httpx.Response:
    """크리덴셜 자동 주입 + Semaphore + 재시도.

    `timeout`을 주면 이 요청만 클라이언트 기본값(_TIMEOUT) 대신 그 값을 쓴다.
    corpCode.xml 처럼 몇 MB짜리 벌크 응답은 작은 JSON 기준으로 잡은 제한을 그대로
    쓰면 안 된다 — 지연이 큰 회선에서는 다 받기도 전에 잘린다.
    """
    client = get_client()
    sem = _get_semaphore()

    merged = {"crtfc_key": require_api_key()}
    if params:
        # None 값 제거 (DART는 빈 파라미터에 민감)
        merged.update({k: v for k, v in params.items() if v is not None})

    # timeout=None 을 httpx 에 그대로 넘기면 "무제한"이 된다 — 안 준 경우와 구분해서
    # 아예 인자를 빼야 클라이언트 기본값이 산다.
    extra = {"timeout": timeout} if timeout is not None else {}

    async with sem:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.get(endpoint, params=merged, **extra)
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < max_retries:
                        backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                        await asyncio.sleep(backoff)
                        continue
                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                if attempt < max_retries:
                    backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                    await asyncio.sleep(backoff)
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("DART request failed without exception")


# DART status code → 사람이 읽을 수 있는 메시지
# https://opendart.fss.or.kr 가이드 참조
_STATUS_MESSAGES = {
    "000": "정상",
    "010": "등록되지 않은 키입니다",
    "011": "사용할 수 없는 키입니다 (오픈API 이용을 신청 후 사용)",
    "012": "접근할 수 없는 IP입니다",
    "013": "조회된 데이터가 없습니다",
    "014": "파일이 존재하지 않습니다",
    "020": "요청 제한을 초과했습니다 (분당 1,000건 / 일 20,000건)",
    "021": "조회 가능한 회사 개수를 초과했습니다 (최대 100건)",
    "100": "필드의 부적절한 값입니다",
    "101": "부적절한 접근입니다",
    "800": "시스템 점검으로 인한 서비스 중단입니다",
    "900": "정의되지 않은 오류가 발생했습니다",
    "901": "사용자 계정의 개인정보 보유기간이 만료되었습니다",
}


async def get_json(endpoint: str, params: dict[str, Any] | None = None) -> dict:
    """JSON 엔드포인트 호출. status가 정상이 아니면 DartApiError."""
    resp = await _request(endpoint, params=params)
    data = resp.json()
    status = str(data.get("status", "")).strip()

    # dartlens_status MCP 도구용 최근 호출 기록 — 진단 부가기능이라 실패해도 본 흐름은 막지 않는다.
    if status:
        from dartlens._metrics import record_dart_call

        try:
            record_dart_call(status)
        except Exception:
            pass

    if status and status != "000":
        message = data.get("message") or _STATUS_MESSAGES.get(status, "알 수 없는 오류")
        raise DartApiError(status, message)
    return data


async def get_multi_acnt(
    corp_codes: list[str],
    bsns_year: int | str,
    reprt_code: str,
) -> list[dict]:
    """fnlttMultiAcnt.json — 다중회사 주요계정 일괄 조회.

    corp_codes는 호출 측에서 ≤100개로 chunk해서 넘긴다 (DART status 021 한도).
    status "013"(조회 결과 없음)은 빈 리스트로 흡수 — 아직 미접수한 회사들이
    섞인 chunk는 정상 케이스다. "020"(요청 제한)은 지수 backoff 3회 재시도.
    그 외 status는 DartApiError로 전파해 호출 측 footer에서 실패 카운트.
    """
    params = {
        "corp_code": ",".join(corp_codes),
        "bsns_year": str(bsns_year),
        "reprt_code": reprt_code,
    }
    for attempt in range(3):
        try:
            data = await get_json("/fnlttMultiAcnt.json", params=params)
            return data.get("list") or []
        except DartApiError as e:
            if e.status == "013":
                return []
            if e.status == "020" and attempt < 2:
                backoff = (2 ** attempt) * 0.5 + random.uniform(0, 0.3)
                await asyncio.sleep(backoff)
                continue
            raise
    return []


# corpCode.xml 은 실측 3.4MB이고 DART 자체가 느려서 한국(왕복 5ms)에서도 3.8초 걸린다
# — 0.9MB/s. 왕복 200ms짜리 해외 회선이면 몇 배로 늘어나고, 작은 JSON 기준인 15초는
# 다 받기 전에 잘린다(2026-08-13 뉴질랜드 문의). 연결 자체는 여전히 빨리 포기하고
# (connect 15초), 본문 받는 시간만 넉넉히 준다.
BULK_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


async def get_bytes(
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: "httpx.Timeout | float | None" = None,
    max_retries: int = 2,
) -> bytes:
    """바이너리 엔드포인트 호출 (corpCode.xml zip, document.xml zip 등)."""
    resp = await _request(endpoint, params=params, timeout=timeout, max_retries=max_retries)
    return resp.content


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None
