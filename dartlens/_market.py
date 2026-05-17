"""KRX 상장사 메타(시장구분·업종·주요제품) — 런타임 fetch + 7일 디스크 캐시.

corpCode.xml에는 시장 구분/업종이 없어 universe="kospi"/"kosdaq" 해석과
섹터 집계를 하려면 외부 소스가 필요하다. KRX KIND 상장법인목록
(corpList.do)은 시장구분·업종(KSIC)·주요제품을 한 번에 주므로
corpCode.xml과 동일한 패턴으로 받아 ~/.dartlens/cache/에 7일 캐시한다.

신규 상장은 캐시 만료(7일) 시 자동 반영 — 별도 유지보수 불필요.
fetch 실패 + 캐시 없음 → 빈 맵, 호출 측이 전체 상장사로 graceful 폴백.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
from lxml import html as _lhtml

from dartlens._metrics import get_data_dir

_CACHE_FILE = "krx_market.json"
_TTL_SECONDS = 7 * 24 * 3600  # corpCode.xml과 동일

_KIND_URL = "http://kind.krx.co.kr/corpgeneral/corpList.do"
# marketType → 정규화된 시장명
_MARKETS = {"stockMkt": "KOSPI", "kosdaqMkt": "KOSDAQ"}

_lock = asyncio.Lock()
_loaded_at: float = 0.0
# "005930" → {"market": "KOSPI", "sector": "통신 및 방송 장비 제조업", "product": "..."}
_by_stock_code: dict[str, dict] = {}


def _cache_path() -> Path:
    cache_dir = get_data_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _CACHE_FILE


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < _TTL_SECONDS


def parse_corp_list(text: str, market_name: str) -> dict[str, dict]:
    """corpList.do HTML 테이블 → {stock_code: {market, sector, product}}.

    순수 함수(네트워크 없는 테스트용). 헤더로 컬럼 인덱스를 찾고,
    포맷 변경 시 표준 레이아웃(0:회사명 1:시장구분 2:종목코드 3:업종
    4:주요제품)으로 폴백한다.
    """
    doc = _lhtml.fromstring(text)
    rows = doc.xpath("//table//tr")
    if not rows:
        return {}
    header = [c.text_content().strip() for c in rows[0].xpath(".//th")]

    def idx(name: str, default: int) -> int:
        try:
            return header.index(name)
        except ValueError:
            return default

    code_idx = idx("종목코드", 2)
    sector_idx = idx("업종", 3)
    product_idx = idx("주요제품", 4)

    out: dict[str, dict] = {}
    for tr in rows[1:]:
        tds = tr.xpath(".//td")
        if len(tds) <= code_idx:
            continue
        code = tds[code_idx].text_content().strip().zfill(6)
        if not (code and code.isdigit()):
            continue
        sector = (
            tds[sector_idx].text_content().strip()
            if len(tds) > sector_idx else ""
        )
        product = (
            tds[product_idx].text_content().strip()
            if len(tds) > product_idx else ""
        )
        out[code] = {"market": market_name, "sector": sector, "product": product}
    return out


async def _download_market(market_type: str) -> dict[str, dict]:
    """corpList.do 한 시장 다운로드 → {stock_code: meta}. 실패 시 예외 전파."""
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True,
        headers={"User-Agent": "dartlens-mcp (+https://github.com/Johnhyeon/dartlens-mcp)"},
    ) as client:
        resp = await client.get(
            _KIND_URL, params={"method": "download", "marketType": market_type}
        )
        resp.raise_for_status()
    # KRX는 EUC-KR(cp949 상위호환) HTML 테이블을 .xls로 내려준다
    text = resp.content.decode("cp949", errors="replace")
    return parse_corp_list(text, _MARKETS[market_type])


def _load_cache(path: Path) -> dict[str, dict] | None:
    """캐시 파일 → entries dict. 구(舊) 포맷('markets')이면 None(재다운로드 유도)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    entries = data.get("entries")
    if isinstance(entries, dict) and entries:
        return entries
    return None  # 구 포맷 or 비어있음


async def ensure_market_loaded(force_refresh: bool = False) -> None:
    """캐시에서 로드하거나 만료/없으면 KRX에서 받아 갱신.

    부분 실패(한 시장만 성공)도 성공분으로 진행한다. 완전 실패 +
    캐시 없음 → _by_stock_code는 빈 채로 둔다(호출 측 폴백 트리거).
    """
    global _loaded_at, _by_stock_code

    async with _lock:
        if _loaded_at and not force_refresh:
            return

        path = _cache_path()
        if not force_refresh and _is_cache_fresh(path):
            cached = _load_cache(path)
            if cached is not None:
                _by_stock_code = cached
                _loaded_at = time.time()
                return
            # 구 포맷/손상 → 재다운로드로 진행

        merged: dict[str, dict] = {}
        for mt in _MARKETS:
            try:
                merged.update(await _download_market(mt))
            except (httpx.HTTPError, ValueError, OSError):
                continue  # 부분 실패 허용

        if merged:
            _by_stock_code = merged
            try:
                path.write_text(
                    json.dumps(
                        {
                            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "entries": merged,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
            _loaded_at = time.time()
        else:
            # 완전 실패: 만료된 옛 캐시라도 entries 포맷이면 사용
            if path.exists():
                cached = _load_cache(path)
                if cached is not None:
                    _by_stock_code = cached
                    _loaded_at = time.time()
                else:
                    _by_stock_code = {}


async def market_stock_codes(market: str) -> set[str]:
    """"KOSPI" 또는 "KOSDAQ"에 속한 종목코드 집합. 매핑 없으면 빈 집합."""
    await ensure_market_loaded()
    target = market.strip().upper()
    return {
        code for code, m in _by_stock_code.items()
        if m.get("market") == target
    }


async def meta_map(stock_codes: list[str]) -> dict[str, dict]:
    """{stock_code: {market, sector, product}}. 없는 코드는 생략.

    scan 결과에 업종/주요제품을 일괄 부착하는 데 쓴다 (조회 O(1)).
    """
    await ensure_market_loaded()
    out: dict[str, dict] = {}
    for sc in stock_codes:
        m = _by_stock_code.get(sc.strip())
        if m:
            out[sc] = m
    return out
