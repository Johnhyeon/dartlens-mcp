"""DART MCP Server — FastMCP 인스턴스 + 도구 선언."""

from __future__ import annotations

# TLS 신뢰 기준(OS 인증서 저장소)을 다른 무엇보다 먼저 세운다. `_http.py` 를 안 타는
# 호출(자가진단·KRX 목록·폐기 목록·업데이트 확인)까지 같은 기준을 쓰게 하려면 여기가
# 가장 이르다.
from dartlens import _tls as _tls_bootstrap

_tls_bootstrap.apply()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from dartlens._cache import cached
from dartlens._corp_code import (
    CorpEntry,
    cache_diagnosis,
    resolve_identifier,
    search_by_name,
)
from dartlens._document_tables import extract_document_tables
from dartlens._earnings import run_scan
from dartlens._earnings_export import run_export
from dartlens._http import get_bytes, get_json
from dartlens import _metrics as _metrics_mod
from dartlens._metrics import read_dart_call_status, track_metrics
from dartlens import _result_meta as rmeta
from datetime import date, timedelta
from dartlens import diagnostics
from dartlens._order_backlog import (
    OrderBacklogSeries,
    extract_order_backlog_point,
    extract_order_backlog_series,
    format_order_backlog_series,
)
from dartlens._safe import DartApiError, safe_tool
from dartlens._validate import (
    days_to_range,
    normalize_bsns_year,
    normalize_corp_code,
    normalize_fs_div,
    normalize_rcept_no,
    normalize_reprt_code,
    normalize_sj_div,
    normalize_yyyymmdd,
    reprt_code_label,
)

mcp = FastMCP(
    "DART",
    instructions="""DART MCP — 금융감독원 전자공시(OpenDART API) 래퍼.

## 정체성

이 서버는 **공시·재무제표 정형 데이터**만 다룹니다. 시세·차트·수급은 자매 서버
`stocklens-mcp`(네이버 증권)가 담당합니다. 두 서버는 서로 호출하지 않으며
Claude가 조정자입니다.

## 식별자 규칙

- **stock_code**: 한국거래소 6자리 종목코드 (예: 005930)
- **corp_code**: DART 8자리 고유번호 (예: 00126380) — 모든 DART API의 키

종목명·종목코드만 알 때 먼저 `search_company`를 호출해 corp_code를 확정하세요.
다른 도구는 corp_code를 입력으로 받습니다.

## 도구

- `search_company`: 종목명/코드 → corp_code + 기업개황
- `list_disclosures`: 기간/유형별 공시 목록 (rcept_no는 후속 도구의 키)
- `get_disclosure_detail`: rcept_no → 공시 본문. 짧은 공시는 발췌, 긴 보고서(사업/분기/반기)는
  인덱스+viewer URL만. `find="키워드"` 인자로 본문 키워드 검색 가능.
- `get_major_accounts`: 정기보고서의 핵심 재무 (매출/영업이익/순이익/자산/부채/자본 등) — 당기·전기·전전기 비교
- `get_full_financial`: 전체 재무제표. sj_div(BS/IS/CIS/CF/SCE) 필수 — 토큰 폭발 방지
- `get_order_backlog`: 사업/분기/반기보고서 표에서 수주잔고·계약잔액 추이를 구조화
- `get_major_holders`: 5%룰 대량보유 변동 — 외국인/펀드/행동주의 진입 추적 (시세에 안 나오는 자본 흐름)
- `get_insider_trades`: 임원·주요주주 특정증권 소유 — 내부자 매매 시그널
- `scan_earnings_season`: 어닝 시즌 유니버스 일괄 스캔 — 채팅용 Top N Markdown
- `export_earnings_scan`: 어닝 시즌 스캔 결과를 XLSX/CSV 파일로 저장 — 한국 Excel은 XLSX 권장
- `dartlens_status`: 자가진단(버전/라이선스/API 키/캐시/업데이트) — 다른 도구가 막힐 때 원인 파악용

## 워크플로우 권장

  1. `search_company("삼성전자")` → corp_code 확보
  2. `list_disclosures(corp_code="00126380", days=30)` → 공시 목록 + rcept_no
  3. `get_disclosure_detail(rcept_no="...")` → 본문 발췌 (필요 시)

또는 재무 흐름:
  1. `search_company` → corp_code
  2. `get_major_accounts(corp_code, bsns_year=2024, reprt_code="annual")` → 빠른 핵심 수치
  3. `get_full_financial(corp_code, bsns_year, reprt_code, fs_div="CFS", sj_div="IS")` → 전체 손익

## 식별자 가이드 (중요)

한국 주식 식별자는 두 가지 — 절대 헷갈리지 마세요:

| 길이 | 형식 | 의미 | 출처 시스템 |
|---|---|---|---|
| 6자리 | 영숫자 (예: `005930`, `0088M0`) | 한국거래소 종목코드 | KRX, 네이버 등 시세 시스템 |
| 8자리 | 숫자만 (예: `00126380`) | DART 고유번호 (corp_code) | 금융감독원 DART |

**디스패치 룰**:
- 사용자가 **8자리 숫자**만 주고 의미를 안 밝히면 → corp_code 가정. `search_company`를 먼저 호출하세요. 결과에 6자리 `stock_code`도 같이 나오니 시세 도구(stocklens 등)에 위임 가능.
- 사용자가 6자리 코드로 DART 정보(공시/재무)를 묻거나, 종목명만 주면 → `search_company`로 corp_code 변환 후 다른 DART 도구 호출.
- 다른 MCP(stocklens 등)가 6자리 코드만 알고 corp_code를 모를 때 → 사용자에게 종목명을 물어 `search_company`로 풀면 됩니다.

`search_company`는 **식별자 변환의 디스패치 허브** 역할도 합니다. 8자리/6자리/이름 무엇이든 받아서 corp_code + stock_code 둘 다 반환.

## 기타 식별자

- reprt_code: "annual"(사업), "Q1"(1분기), "H1"(반기), "Q3"(3분기) — 한글 라벨도 인식.
- fs_div: "CFS"(연결재무제표 — 기본), "OFS"(별도재무제표).
- sj_div: "BS"(재무상태표), "IS"(손익계산서), "CIS"(포괄손익), "CF"(현금흐름표), "SCE"(자본변동표).

## 🚨 분기·반기 손익: 3개월 vs 누적 (반드시 컬럼명을 읽으세요)

DART 분기/반기 보고서의 손익은 두 가지 금액이 함께 존재합니다. 표의 컬럼명이
어느 쪽인지 이미 말해주고 있으니 **컬럼명을 그대로 인용**하세요.

- `2분기(3개월)` / `3분기(3개월)` — 그 분기 3개월치만
- `상반기 누적` / `3분기 누적` — 연초부터 쌓인 값

반기보고서를 조회했다고 그 숫자가 상반기 누적인 게 아닙니다. 예: 디오 2026 반기
매출은 2분기 449억 / 상반기 누적 862억으로 컬럼이 따로 나옵니다. "상반기 매출"을
물었으면 **누적 컬럼**을, "2분기 실적"을 물었으면 3개월 컬럼을 쓰세요.

재무상태표는 시점 값이라 누적이 없고, 전기 컬럼은 전년 **말**(12/31)입니다 —
전년 반기말이 아닙니다. 손익의 전기 컬럼만 전년 동기입니다.

`⚠️ 3개월/누적 구분이 불가` 경고가 붙으면 그 회사는 누적을 제출하지 않은 것이니
금액을 누적으로 단정하지 말고 `get_disclosure_detail`로 원문 매출실적표를 대조하세요.

## 🕐 결과 메타 봉투 (RESULT_META_JSON) — 기준일과 이어달리기

대부분의 도구는 응답 끝에 `RESULT_META_JSON_START…END` 블록을 붙입니다.

- `data_as_of` = **근거 공시의 접수일**. "이 실적은 언제 공시된 것인가"의 답입니다.
  `as_of`(조회 시각)와 혼동하지 마세요. 사용자에게 말할 날짜는 `data_as_of`입니다.
- `data_period` = 그 숫자가 덮는 기간(예: `2026 반기보고서`). 날짜가 아니라 기간이
  기준인 재무 데이터는 여기를 보세요.
- `entity`에 **corp_code(8자리)와 stock_code(6자리)가 함께** 들어 있습니다.
  - 시세·수급·차트로 넘어갈 때 `stock_code`를 그대로 쓰세요 (StockLens `code=`).
  - 종목을 다시 검색하거나 코드를 추측하지 마세요.
  - 공시 반응을 볼 때는 `data_as_of`를 그대로 `event_date=`로 넘기면 됩니다.
- `warnings`를 반드시 읽으세요. 특히:
  - `find=` 매치 0건 → **"본문에 없다"는 뜻이 아닙니다.** 부정 결론 금지.
  - 긴 보고서 본문 미반환 → 학습지식으로 메우지 말고 `find=`로 다시 조회하세요.
- 메타 블록은 내부용입니다. JSON을 사용자에게 그대로 보여주지 말고 필요한 사실만
  문장으로 옮기세요.

## 📌 정정공시

정기보고서는 나중에 정정되는 일이 흔합니다(최근 표본에서 5건 중 1건). 재무 조회
결과에 `⚠️ 이 보고서는 **정정공시가 있습니다**`가 붙으면:

- 표의 수치는 **정정 반영본이라 지금은 맞습니다.** 숫자를 의심하지 마세요.
- 다만 사용자가 **예전에 받아둔 값과 다를 수 있다**는 사실을 함께 알려주세요.
- 무엇이 어떻게 바뀌었는지 확인하려면 안내된 rcept_no로 `get_disclosure_detail`을
  부르세요. 정정 사유와 정정 전후 대비표가 본문에 들어 있습니다.
- 경고가 없으면 그 보고서에 정정이 없다는 뜻입니다. 굳이 언급하지 마세요.
""",
)


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

@cached(ttl_seconds=24 * 3600)
async def _fetch_company(corp_code: str) -> dict:
    """DART company.json — 기업개황. 24시간 캐시."""
    return await get_json("/company.json", params={"corp_code": corp_code})


_CORP_CLS_LABEL = {
    "Y": "유가증권",
    "K": "코스닥",
    "N": "코넥스",
    "E": "기타",
}


def _format_company(entry: CorpEntry, profile: dict) -> str:
    cls_code = (profile.get("corp_cls") or "").strip()
    cls_label = _CORP_CLS_LABEL.get(cls_code, cls_code or "-")

    lines = [
        f"# {profile.get('corp_name') or entry.corp_name}",
        "",
        f"- corp_code: `{entry.corp_code}` (DART 고유번호)",
        f"- 종목코드: {entry.stock_code or '비상장'}",
        f"- 시장구분: {cls_label}",
        f"- 영문명: {profile.get('corp_name_eng') or entry.corp_eng_name or '-'}",
        f"- 대표자: {profile.get('ceo_nm') or '-'}",
        f"- 설립일: {profile.get('est_dt') or '-'}",
        f"- 결산월: {profile.get('acc_mt') or '-'}",
        f"- 사업자번호: {profile.get('bizr_no') or '-'}",
        f"- 법인등록번호: {profile.get('jurir_no') or '-'}",
        f"- 업종코드: {profile.get('induty_code') or '-'}",
        f"- 주소: {profile.get('adres') or '-'}",
        f"- 홈페이지: {profile.get('hm_url') or '-'}",
        f"- IR페이지: {profile.get('ir_url') or '-'}",
        f"- 전화: {profile.get('phn_no') or '-'} / 팩스: {profile.get('fax_no') or '-'}",
    ]
    return "\n".join(lines)


def _format_candidates(entries: list[CorpEntry], query: str) -> str:
    lines = [
        f"'{query}' 검색 결과 ({len(entries)}건). 정확한 회사를 골라 다시 호출하세요.",
        "",
    ]
    for e in entries:
        market = ""
        if e.stock_code:
            market = f" [{e.stock_code}]"
        lines.append(f"- {e.corp_name}{market} → corp_code: `{e.corp_code}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
@safe_tool
@track_metrics("search_company")
async def search_company(query: str, listed_only: bool = True) -> str:
    """기업검색 / 식별자 변환 디스패치 — 종목명·6자리 종목코드·8자리 corp_code 무엇이든 받아 corp_code + stock_code + 기업개황 반환.

    DART 후속 API는 모두 8자리 corp_code를 입력으로 받으므로, 워크플로우 첫 디스패치로 자주 쓰임.
    종목명/6자리 코드만 알 때 corp_code 확보용. 8자리 숫자는 (종목코드 6자리와 달리) corp_code로 가정.

    입력 자동 판정:
        8자리 숫자 → corp_code / 6자리 영숫자 → stock_code / 그 외 → 종목명 검색(정확·부분)

    Args:
        query: 종목명, 6자리 종목코드, 또는 8자리 corp_code.
        listed_only: True면 상장사만 (기본). 비상장 자회사도 포함하려면 False.
    """
    q = (query or "").strip()
    if not q:
        return "⚠️ 검색어가 비어있습니다."

    # 1) 정확한 코드 매칭 시도
    entry = await resolve_identifier(q)
    if entry is not None:
        profile = await _fetch_company(entry.corp_code)
        return rmeta.append_meta(_format_company(entry, profile), _identity_meta(entry))

    # 2) 이름 검색
    candidates = await search_by_name(q, listed_only=listed_only, limit=20)
    if not candidates:
        # listed_only=True 였고 결과 없으면 비상장도 한 번 더 시도
        if listed_only:
            fallback = await search_by_name(q, listed_only=False, limit=10)
            if fallback:
                lines = [
                    f"'{q}' 상장사에서는 결과가 없습니다.",
                    f"비상장 포함 후보 {len(fallback)}건:",
                    "",
                ]
                for e in fallback:
                    tag = "비상장" if not e.is_listed else e.stock_code
                    lines.append(f"- {e.corp_name} [{tag}] → corp_code: `{e.corp_code}`")
                return "\n".join(lines)
        return f"'{q}'에 해당하는 회사를 찾을 수 없습니다. 정확한 종목명/코드를 확인해주세요."

    if len(candidates) == 1:
        entry = candidates[0]
        profile = await _fetch_company(entry.corp_code)
        return rmeta.append_meta(_format_company(entry, profile), _identity_meta(entry))

    return _format_candidates(candidates, q)


# ---------------------------------------------------------------------------
# list_disclosures
# ---------------------------------------------------------------------------

# 공시유형 친근 라벨 → DART pblntf_ty 코드
# https://opendart.fss.or.kr 가이드 참조 (정기/주요사항/발행/지분/외부감사/거래소 등)
_KIND_TO_PBLNTF_TY: dict[str, str] = {
    "all": "",
    "regular": "A", "정기": "A",          # 사업/반기/분기보고서
    "material": "B", "주요사항": "B",      # 주요사항보고서 (감자, M&A 등)
    "issuance": "C", "발행": "C",          # 증권신고
    "ownership": "D", "지분": "D",         # 대량보유, 임원·주요주주
    "etc": "E", "기타": "E",
    "audit": "F", "외부감사": "F", "감사": "F",
    "fund": "G", "펀드": "G",
    "abs": "H", "자산유동화": "H",
    "exchange": "I", "거래소": "I",
    "fair": "J", "공정위": "J",
}


def _resolve_kind(kind: str) -> str:
    k = (kind or "all").strip().lower()
    # 한글은 lower 영향 없음
    if k in _KIND_TO_PBLNTF_TY:
        return _KIND_TO_PBLNTF_TY[k]
    # 사용자가 raw DART 코드(A~J)를 직접 줬을 수도
    if len(k) == 1 and k.upper() in {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J"}:
        return k.upper()
    raise ValueError(
        f"알 수 없는 공시유형 '{kind}'. "
        f"사용 가능: all, regular, material, issuance, ownership, audit, exchange (또는 한글 라벨)."
    )


@cached(ttl_seconds=5 * 60)
async def _fetch_disclosure_list(
    corp_code: str | None,
    bgn_de: str,
    end_de: str,
    pblntf_ty: str,
    page_count: int,
) -> dict:
    params: dict = {
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_no": 1,
        "page_count": page_count,
        "sort": "date",
        "sort_mth": "desc",
    }
    if corp_code:
        params["corp_code"] = corp_code
    if pblntf_ty:
        params["pblntf_ty"] = pblntf_ty
    try:
        return await get_json("/list.json", params=params)
    except DartApiError as e:
        # status 013 = "조회된 데이터가 없습니다" — 에러가 아니라 빈 결과로 취급
        if e.status == "013":
            return {"status": "013", "message": e.message, "list": [], "total_count": 0}
        raise


def _format_disclosures(
    data: dict,
    *,
    corp_code: str | None,
    bgn_de: str,
    end_de: str,
    kind: str,
) -> str:
    items = data.get("list") or []
    total = data.get("total_count") or len(items)
    bgn_fmt = f"{bgn_de[:4]}-{bgn_de[4:6]}-{bgn_de[6:]}"
    end_fmt = f"{end_de[:4]}-{end_de[4:6]}-{end_de[6:]}"

    scope = f"corp_code={corp_code}" if corp_code else "전체회사"
    kind_label = kind if kind != "all" else "전체유형"
    header = f"# 공시 목록 ({scope}, {bgn_fmt} ~ {end_fmt}, {kind_label}, {total}건)"

    if not items:
        return header + "\n\n해당 기간/조건에 공시가 없습니다."

    lines = [header, "", "| 접수일 | 회사 | 보고서명 | rcept_no | 비고 |", "|---|---|---|---|---|"]
    for r in items:
        rcept_dt = r.get("rcept_dt") or ""
        date_fmt = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}" if len(rcept_dt) == 8 else rcept_dt
        corp = r.get("corp_name") or ""
        report = (r.get("report_nm") or "").replace("|", "·")
        rcept_no = r.get("rcept_no") or ""
        rm = (r.get("rm") or "").strip() or "-"
        lines.append(f"| {date_fmt} | {corp} | {report} | `{rcept_no}` | {rm} |")

    if len(items) < total:
        lines.append("")
        lines.append(f"_표시 {len(items)}건 / 전체 {total}건. 더 많은 결과는 days·limit 조정._")
        lines.append(
            "_이 목록은 전체가 아닙니다. 빠짐없이 보려면 기간(bgn_de·end_de)을 좁히거나 "
            "kind 로 공시유형을 지정해 나눠 조회하세요._"
        )
    lines.append("")
    lines.append("_rcept_no는 향후 get_disclosure_detail 도구의 입력값으로 사용됩니다._")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("list_disclosures")
async def list_disclosures(
    corp_code: str | None = None,
    days: int = 30,
    kind: str = "all",
    limit: int = 20,
    bgn_de: str | None = None,
    end_de: str | None = None,
) -> str:
    """공시목록 — DART에 접수된 공시 리스트를 기간·유형으로 필터링.

    특정 회사 공시는 corp_code(8자리) 필수. 종목명/코드만 알면 먼저 search_company로 확정.
    corp_code 생략 시 전체 회사 공시. (rcept_no는 후속 도구 입력값)

    Args:
        corp_code: DART 8자리 고유번호 (선택). 생략 시 전체회사 공시.
        days: 오늘 기준 최근 N일 (기본 30, 최대 3650). bgn_de/end_de 주면 무시.
        kind: 공시유형. "all"/"regular"(정기)/"material"(주요사항)/"issuance"(발행)/
            "ownership"(지분)/"audit"(외부감사)/"exchange"(거래소). 한글 라벨·DART 코드(A~J)도 가능.
        limit: 반환 건수 (기본 20, 최대 100).
        bgn_de: 시작일 YYYYMMDD (선택, days보다 우선).
        end_de: 종료일 YYYYMMDD (선택, days보다 우선).
    """
    cc = normalize_corp_code(corp_code) if corp_code is not None else None
    pblntf_ty = _resolve_kind(kind)

    if not isinstance(limit, int) or limit < 1 or limit > 100:
        raise ValueError(f"limit은 1~100 사이의 정수여야 합니다 (받음: {limit}).")

    if bgn_de or end_de:
        bgn = normalize_yyyymmdd(bgn_de, field="bgn_de")
        end = normalize_yyyymmdd(end_de, field="end_de")
        if bgn > end:
            raise ValueError(f"bgn_de({bgn})가 end_de({end})보다 늦습니다.")
    else:
        bgn, end = days_to_range(days)

    data = await _fetch_disclosure_list(cc, bgn, end, pblntf_ty, limit)
    items = data.get("list") or []

    # DART는 조건에 맞는 전체 건수를 total_count 로 알려준다. limit 에 걸려 잘린
    # 응답에 complete 가 붙어 있으면, 2,894건 중 20건을 보고 "최근 1년 공시를 다
    # 봤다"고 읽는다. 본문의 "표시 N건 / 전체 M건"은 그 줄을 안 읽으면 그만이다.
    raw_total = data.get("total_count")
    try:
        total = int(raw_total) if raw_total is not None else None
    except (TypeError, ValueError):
        total = None

    if total is None:
        # 전체 건수를 모르면 다 봤는지도 알 수 없다. 모른다고 적는다.
        truncated, complete, reason = False, False, "unknown"
    else:
        truncated = len(items) < total
        complete = not truncated
        reason = "pagination" if truncated else None

    coverage = {
        "requested": {"unit": "item", "value": limit},
        "effective": {"unit": "item", "value": len(items)},
        "returned_count": len(items),
        "total_count": total,
        "truncated": truncated,
        "coverage_complete": complete,
        "reason": reason,
    }
    if items:
        completeness = rmeta.COMPLETE if complete else rmeta.PARTIAL
    else:
        completeness = rmeta.NONE

    return rmeta.append_meta(
        _format_disclosures(data, corp_code=cc, bgn_de=bgn, end_de=end, kind=kind),
        _dart_meta(
            rows=items, corp_code=cc,
            data_period=f"{bgn} ~ {end}",
            data_completeness=completeness,
            coverage=coverage,
        ),
    )


# ---------------------------------------------------------------------------
# get_major_accounts (fnlttSinglAcnt.json)
# ---------------------------------------------------------------------------

# 주요계정 응답에 자주 등장하는 계정명 정렬 키 — 의미 있는 순서로 보여주기 위함
_ACCOUNT_ORDER = {
    # IS / CIS
    "매출액": 1, "영업수익": 1,
    "매출원가": 2,
    "매출총이익": 3,
    "판매비와관리비": 4,
    "영업이익": 5, "영업이익(손실)": 5,
    "영업외수익": 6,
    "영업외비용": 7,
    "법인세비용차감전순이익": 8, "법인세비용차감전순이익(손실)": 8,
    "법인세비용": 9,
    "당기순이익": 10, "당기순이익(손실)": 10,
    # BS
    "자산총계": 100,
    "유동자산": 101,
    "비유동자산": 102,
    "부채총계": 110,
    "유동부채": 111,
    "비유동부채": 112,
    "자본총계": 120,
    "자본금": 121,
    "이익잉여금": 122,
}


@cached(ttl_seconds=24 * 3600)
async def _fetch_major_accounts(corp_code: str, bsns_year: str, reprt_code: str) -> dict:
    try:
        return await get_json(
            "/fnlttSinglAcnt.json",
            params={
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
            },
        )
    except DartApiError as e:
        if e.status == "013":
            return {"status": "013", "message": e.message, "list": []}
        raise


def _fmt_amount(value) -> str:
    """천단위 콤마. 음수/빈값 안전 처리. 주식수·EPS·작은 금액용."""
    if value is None:
        return "-"
    s = str(value).strip()
    if not s or s == "-":
        return "-"
    # DART는 음수를 '-숫자' 또는 괄호로 줄 수 있음
    sign = ""
    if s.startswith("-"):
        sign = "-"
        s = s[1:]
    if s.startswith("(") and s.endswith(")"):
        sign = "-"
        s = s[1:-1]
    digits = s.replace(",", "")
    if digits.replace(".", "").isdigit():
        try:
            if "." in digits:
                return sign + f"{float(digits):,.2f}"
            return sign + f"{int(digits):,}"
        except ValueError:
            pass
    return (sign + s)


def _fmt_won(value) -> str:
    """한국 원 단위 압축 — 큰 숫자 토큰 효율 개선용 (재무 표 전용).

    토큰 효율: '300,870,903,000,000' → '300조 8,709억' (~9 토큰 → ~5 토큰).

    규칙:
    - 1조(10^12) 이상: 'N조 M억' (M=0이면 '조'만)
    - 1억(10^8) 이상: 'N억' (억 미만 절사 — 가독성 우선)
    - 그 미만 또는 소수점 포함: _fmt_amount fallback (콤마)

    매출/영업이익/자산 등 재무 표에 적용. 주식수·EPS·증감 컬럼은 _fmt_amount 그대로.
    """
    if value is None:
        return "-"
    s = str(value).strip()
    if not s or s == "-":
        return "-"
    sign = ""
    if s.startswith("-"):
        sign, s = "-", s[1:]
    elif s.startswith("(") and s.endswith(")"):
        sign, s = "-", s[1:-1]
    digits = s.replace(",", "")
    if not digits.isdigit():
        # 소수점 / 비숫자 → 그대로 콤마 처리
        return _fmt_amount(value)

    n = int(digits)
    JO = 1_000_000_000_000
    EOK = 100_000_000

    if n >= JO:
        jo = n // JO
        eok = (n % JO) // EOK
        if eok > 0:
            return f"{sign}{jo:,}조 {eok:,}억"
        return f"{sign}{jo:,}조"
    if n >= EOK:
        return f"{sign}{n // EOK:,}억"
    return f"{sign}{n:,}"


# 정기보고서 정정공시는 이름에 대상 기간이 붙는다: "[기재정정]반기보고서 (2026.06)".
# reprt_code → 그 기간 표기의 월. 이걸로 "이 재무수치의 근거 보고서가 정정됐는지"를
# 가린다. 최근 5일 공시 100건 중 20건이 정정이었고, 부방은 2024 사업보고서를
# 2026-08-14에 정정했다 — 1년 8개월 뒤다. 그 사이 조회한 사람은 다른 숫자를 받았다.
_REPRT_PERIOD_MONTH = {"11011": "12", "11013": "03", "11012": "06", "11014": "09"}

# 정정을 얼마나 거슬러 찾을지. 기간 종료일부터 오늘까지 훑되 3년으로 자른다.
_CORRECTION_LOOKBACK_YEARS = 3


@cached(ttl_seconds=6 * 3600)
async def _fetch_corrections(corp_code: str, bgn_de: str, end_de: str) -> list[dict]:
    """해당 회사의 정기공시(A) 중 정정 건만.

    조회 실패를 빈 목록으로 바꾸지 않는다. 실패를 [] 로 삼키면 "정정 없음"과
    구분이 사라져, 정정된 보고서를 정정 없는 것으로 보여주게 된다. 부르는 쪽이
    "확인 못 했다"고 적을 수 있어야 한다.
    """
    try:
        data = await get_json(
            "/list.json",
            params={
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": "A",
                "page_count": "100",
            },
        )
    except DartApiError as e:
        if e.status == "013":   # 조회된 데이터 없음 = 정말 정정이 없다
            return []
        raise
    return [r for r in (data.get("list") or []) if "정정" in (r.get("report_nm") or "")]


async def find_correction(corp_code: str, bsns_year: str, reprt_code: str) -> dict | None:
    """이 정기보고서에 대한 정정공시가 있으면 가장 최근 건을 돌려준다.

    DART API는 정정 반영본을 주므로 **숫자 자체는 최신이 맞다.** 문제는 그게
    정정된 값이라는 사실을 말할 방법이 없다는 것. 예전에 같은 조회를 한 사람은
    다른 숫자를 봤고, 그걸 알 방법이 없었다.
    """
    month = _REPRT_PERIOD_MONTH.get(reprt_code)
    if not month:
        return None
    marker = f"({bsns_year}.{month})"

    period_end = date(int(bsns_year), int(month), 28)
    today = date.today()
    if period_end > today:
        return None
    bgn = max(period_end, today - timedelta(days=365 * _CORRECTION_LOOKBACK_YEARS))

    rows = await _fetch_corrections(
        corp_code, bgn.strftime("%Y%m%d"), today.strftime("%Y%m%d")
    )
    hits = [r for r in rows if marker in (r.get("report_nm") or "")]
    if not hits:
        return None
    return max(hits, key=lambda r: (r.get("rcept_dt", ""), r.get("rcept_no", "")))


_SCOPE_MIX_NOTE = (
    "⚠️ 연결(CFS)과 별도(OFS)가 **한 응답에 같이** 들어 있습니다. 두 범위를 합산하면 "
    "존재하지 않는 회사의 숫자가 됩니다. 한쪽을 골라 쓰고, 어느 쪽인지 함께 적으세요."
)


def _financial_scope(rows: list[dict] | None, requested_fs: str | None = None) -> dict:
    """이 응답에 어떤 재무 범위가 들어 있는가.

    fnlttSinglAcnt.json 은 연결과 별도를 한 번에 준다. 표는 나눠 그리지만
    메타에는 그 사실이 없어서, 행을 그대로 집계하는 소비자가 둘을 더해버린다.
    """
    present = sorted({(r.get("fs_div") or "").strip() for r in (rows or [])} - {""})
    # fnlttSinglAcntAll.json 행에는 fs_div 가 없다(실측: 141행 전부 None). 이
    # 엔드포인트는 요청 인자로 범위를 가르므로, 표기가 없다고 비워 두면 "무슨
    # 범위인지 모른다"로 읽힌다. 실제로는 요청한 그 범위다.
    if not present and rows and requested_fs:
        present = [requested_fs]
    if "CFS" in present:
        preferred = "CFS"
    elif present:
        preferred = present[0]
    else:
        preferred = requested_fs
    currency = ""
    for r in rows or []:
        currency = (r.get("currency") or "").strip()
        if currency:
            break
    return {
        "scopes_present": present,
        "preferred_scope": preferred,
        "scope_mixed_in_response": len(present) > 1,
        "currency": currency or "KRW",
    }


def _filing_day(rows: list[dict] | None) -> str | None:
    """행들의 접수일 중 가장 최근 날짜(YYYY-MM-DD). 없으면 None."""
    days = []
    for r in rows or []:
        d = str(r.get("rcept_dt") or "").strip()
        if not (len(d) == 8 and d.isdigit()):
            n = str(r.get("rcept_no") or "")
            d = n[:8] if len(n) >= 8 and n[:8].isdigit() else ""
        if d:
            days.append(d)
    return rmeta.normalize_day(max(days)) if days else None


async def _check_correction(corp_code: str, bsns_year: str, reprt_code: str):
    """정정 조회를 결과와 실패로 나눠 돌려준다: (correction, checked, error_name).

    조회가 깨졌는데 None 을 돌려주면 "정정 없음"과 똑같이 보인다. 그러면 정정된
    보고서를 정정 없는 것으로 안내하게 된다.
    """
    try:
        return await find_correction(corp_code, bsns_year, reprt_code), True, None
    except Exception as e:
        return None, False, type(e).__name__


def _filing_state(
    *, bsns_year: str, reprt_code: str, rows: list[dict] | None,
    correction: dict | None, checked: bool,
) -> dict:
    day = None
    if correction:
        day = rmeta.normalize_day(
            correction.get("rcept_dt") or correction.get("rcept_no")
        )
    return {
        "business_year": bsns_year,
        "report_code": reprt_code,
        "filing_date": _filing_day(rows),
        "correction_checked": checked,
        "correction_applied": bool(correction),
        "latest_correction_date": day,
    }


def _correction_unchecked_note(error_name: str | None) -> str:
    return (
        "정정공시 확인에 실패했습니다"
        + (f"({error_name})" if error_name else "")
        + ". 이 수치가 정정 반영본인지 **확인되지 않았습니다** - "
        "정정이 없다는 뜻이 아닙니다."
    )


def _correction_note(correction: dict | None) -> str | None:
    if not correction:
        return None
    dt = (correction.get("rcept_dt") or "")
    dt = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}" if len(dt) == 8 else dt
    return (
        f"⚠️ 이 보고서는 **정정공시가 있습니다** — {dt} `{correction.get('report_nm')}` "
        f"(rcept_no=`{correction.get('rcept_no')}`). 위 수치는 정정 반영본이지만, "
        "예전에 받아둔 값과 다를 수 있습니다. 원문 대조가 필요하면 이 접수번호를 보세요."
    )


def _identity_meta(entry) -> dict:
    """search_company 전용 — 식별자 변환 결과 자체가 이 도구의 산출물이다.

    corp_code(8) ↔ stock_code(6) 대응은 시점 데이터가 아니라 매핑이므로 filing
    기준일이 없다. 대신 entity를 채워, 이어지는 StockLens/TelegramLens 호출이
    6자리 코드를 다시 찾거나 추측하지 않게 한다.
    """
    return rmeta.build_meta(
        lens="dartlens",
        data_basis=rmeta.BASIS_FILING,
        market="KR",
        entity_info=rmeta.entity(
            stock_code=getattr(entry, "stock_code", None),
            corp_code=getattr(entry, "corp_code", None),
            name=getattr(entry, "corp_name", None),
        ),
    )


def _emit_coverage_counters(lens: str, meta: dict) -> None:
    """메타에 이미 담긴 한계 사실을 카운터로 옮긴다.

    조건을 다시 계산하지 않는다. 응답에 실린 것과 세는 것이 어긋나면 지표를
    믿을 수 없다. 라벨은 고정 집합이라 종목코드·검색어는 들어가지 않는다.
    """
    tool = _metrics_mod.current_tool()
    if not tool:
        return
    try:
        cov = meta.get("coverage") or {}
        if cov.get("truncated") is True:
            _metrics_mod.count_limitation(
                "lens_coverage_truncated_total",
                lens=lens, tool=tool, reason=str(cov.get("reason") or "unknown"),
            )
        bar = meta.get("bar_state") or {}
        if bar.get("calculation_includes_incomplete") is True:
            _metrics_mod.count_limitation(
                "lens_incomplete_bar_total",
                tool=tool, timeframe=str(bar.get("timeframe") or "unknown"),
            )
        if (meta.get("period_coverage") or {}).get("consistency") == "mixed":
            _metrics_mod.count_limitation("lens_mixed_period_total", tool=tool)
        if (meta.get("price_adjustment") or {}).get("status") == "unknown":
            _metrics_mod.count_limitation("lens_unknown_adjustment_total", tool=tool)
    except Exception:
        # 지표 때문에 도구 응답이 죽으면 안 된다.
        pass


def _dart_meta(
    *,
    rows: list[dict] | None = None,
    corp_code: str | None = None,
    stock_code: str | None = None,
    name: str | None = None,
    rcept_no: str | None = None,
    rcept_dt: str | None = None,
    data_period: str | None = None,
    data_completeness: str = rmeta.COMPLETE,
    coverage: dict | None = None,
    extra: dict | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """DART 도구용 결과 메타. 기준일은 **근거 공시의 접수일**이다.

    DART 응답 행에는 corp_code와 stock_code가 함께 들어 있다(예: 00115931 /
    039840). 이 둘을 메타로 흘려주면 StockLens·TelegramLens가 종목 재검색 없이
    바로 이어받을 수 있고, 코드를 추측할 유인이 사라진다.

    rcept_dt를 안 주면 행들의 접수일 중 **가장 최근 날짜**를 쓴다.

    rows[0]이 항상 최신인 것은 아니다 — 대량보유(5%룰)·임원소유 목록은 DART가
    오래된 순으로 돌려주는 경우가 있어, 첫 행을 기준일로 삼으면 표에는 올해
    공시가 떠 있는데 메타만 몇 년 전을 가리킨다(실측: 표 최신 2026-07-01,
    메타 2024-09-12). 기준일이 틀리면 소비자가 최신 공시를 과거 것으로 옮겨 적는다.
    """
    first = (rows or [{}])[0] if rows else {}

    def _row_day(row: dict) -> str | None:
        d = str(row.get("rcept_dt") or "").strip()
        if len(d) == 8 and d.isdigit():
            return d
        n = str(row.get("rcept_no") or "")
        return n[:8] if len(n) >= 8 and n[:8].isdigit() else None

    day = rcept_dt
    if not day and rows:
        days = [d for d in (_row_day(r) for r in rows) if d]
        day = max(days) if days else None
    if not day:
        no = rcept_no or (first.get("rcept_no") or "")
        day = no[:8] if len(no) >= 8 and no[:8].isdigit() else None

    meta = rmeta.build_meta(
        lens="dartlens",
        data_basis=rmeta.BASIS_FILING,
        data_as_of=day,
        data_period=data_period,
        market="KR",
        data_completeness=data_completeness,
        coverage=coverage,
        entity_info=rmeta.entity(
            stock_code=stock_code or first.get("stock_code"),
            corp_code=corp_code or first.get("corp_code"),
            name=name or first.get("corp_name"),
        ),
        warnings=warnings,
    )
    # 판정에 영향을 주지 않는 v3 부가 필드(match_coverage 등).
    for key, value in (extra or {}).items():
        meta[key] = value
    _emit_coverage_counters("dartlens", meta)
    return meta


def _has_amount(value) -> bool:
    """DART 금액 필드에 실제 값이 있는지. 결측은 None / "" / "-" 셋 다 온다."""
    if value is None:
        return False
    return str(value).strip() not in ("", "-")


def _amount_key(value) -> str:
    """금액 동일성 비교용 — 콤마·공백 표기 차이 무시."""
    return str(value or "").replace(",", "").strip()


# fnlttSinglAcntAll은 분기/반기 손익의 전년동기를 frmtrm_amount가 아니라
# frmtrm_q_amount로 내려보낸다. 한쪽만 읽으면 전기 컬럼이 통째로 비어버린다.
_PREV_KEYS = ("frmtrm_amount", "frmtrm_q_amount")


def _pick_amount(row: dict, keys: tuple[str, ...]):
    """후보 필드 중 값이 있는 첫 번째."""
    for k in keys:
        v = row.get(k)
        if _has_amount(v):
            return v
    return None


# 분기·반기 정기보고서 손익 컬럼 라벨 (3개월 / 누적 / 전년 3개월 / 전년 누적).
# DART는 thstrm_amount에 '해당 3개월', thstrm_add_amount에 '당해 누적'을 담으면서
# thstrm_nm으로는 보고서 종류명("제 39 기 반기")만 준다. 그 라벨을 그대로 붙이면
# 3개월 값이 누적치로 읽힌다 (디오 2026 반기 매출: 3개월 449억 / 누적 863억).
_QUARTER_COLS = {
    "11013": ("1분기(3개월)", "1분기 누적", "전년 1분기", "전년 1분기 누적"),
    "11012": ("2분기(3개월)", "상반기 누적", "전년 2분기", "전년 상반기"),
    "11014": ("3분기(3개월)", "3분기 누적", "전년 3분기", "전년 3분기 누적"),
}

_CUM_MISSING_NOTE = (
    "_⚠️ 이 보고서는 손익에 누적 컬럼이 없어 3개월/누적 구분이 불가합니다. "
    "위 금액을 누적으로 단정하지 말고 DART 원문 표를 대조하세요._"
)


def _period_columns(
    rows: list[dict], reprt_code: str
) -> tuple[list[tuple[str, tuple[str, ...]]], bool]:
    """이 표에 쓸 (컬럼 헤더, 값 후보 필드) 목록과 '3개월/누적 구분 불가' 플래그.

    같은 보고서 안에서도 재무상태표(시점)와 손익계산서(기간)는 기간이 다르다.
    반기보고서 BS의 전기는 '제38기말'(전년 12/31)인데 IS의 전기는 '제38기 반기'다.
    라벨을 보고서 전체에 하나로 뭉치면 BS 비교 시점이 틀리므로 표 단위로 호출한다.
    """

    def first_nm(*keys: str) -> str:
        for k in keys:
            for r in rows:
                v = (r.get(k) or "").strip()
                if v:
                    return v
        return ""

    is_income = any((r.get("sj_div") or "").strip() in ("IS", "CIS") for r in rows)
    has_cum = any(_has_amount(r.get("thstrm_add_amount")) for r in rows)
    # 1분기는 3개월 = 누적이라 두 값이 같다. 같은 숫자를 두 컬럼에 낼 이유가 없다.
    cum_differs = any(
        _has_amount(r.get("thstrm_add_amount"))
        and _amount_key(r["thstrm_add_amount"]) != _amount_key(r.get("thstrm_amount"))
        for r in rows
    )

    if has_cum and cum_differs and reprt_code in _QUARTER_COLS:
        q, cum, prev_q, prev_cum = _QUARTER_COLS[reprt_code]
        return [
            (q, ("thstrm_amount",)),
            (cum, ("thstrm_add_amount",)),
            (prev_q, _PREV_KEYS),
            (prev_cum, ("frmtrm_add_amount",)),
        ], False

    cols = [
        (first_nm("thstrm_nm") or "당기", ("thstrm_amount",)),
        (first_nm("frmtrm_nm", "frmtrm_q_nm") or "전기", _PREV_KEYS),
    ]
    if any(_has_amount(r.get("bfefrmtrm_amount")) for r in rows):
        cols.append((first_nm("bfefrmtrm_nm") or "전전기", ("bfefrmtrm_amount",)))

    # 반기/3분기인데 누적 컬럼이 아예 없으면 thstrm_amount가 3개월인지 누적인지 모른다.
    return cols, is_income and reprt_code in ("11012", "11014") and not has_cum


def _render_amount_table(
    rows: list[dict], cols: list[tuple[str, tuple[str, ...]]]
) -> list[str]:
    lines = [
        "| 계정 | " + " | ".join(h for h, _ in cols) + " |",
        "|---" + "|---:" * len(cols) + "|",
    ]
    for r in rows:
        acc = (r.get("account_nm") or "").strip() or "(이름없음)"
        cells = " | ".join(_fmt_won(_pick_amount(r, keys)) for _, keys in cols)
        lines.append(f"| {acc} | {cells} |")
    return lines


def _dedup_account_rows(items: list[dict]) -> list[dict]:
    """DART fnlttSinglAcnt가 동일 항목을 두 번 내려보내는 노이즈 제거.

    예: 삼성전자 사업보고서에서 '당기순이익(손실)'이 IS 안에 ord=29와 ord=61로 두 번 박힘.
    fs_div, sj_div, account_nm, 모든 amount가 100% 같음 (ord만 다름).

    안전을 위해 전 키가 정확히 일치할 때만 dedup. amount 한 글자라도 다르면 보존
    (지배/비지배 구분 같은 의미 있는 행일 수 있음). 낮은 ord 우선 보존.
    """
    seen: dict[tuple, tuple[int, dict]] = {}
    for r in items:
        key = (
            (r.get("fs_div") or "").strip(),
            (r.get("sj_div") or "").strip(),
            (r.get("account_nm") or "").strip(),
            (r.get("thstrm_amount") or "").strip(),
            (r.get("thstrm_add_amount") or "").strip(),
            (r.get("frmtrm_amount") or "").strip(),
            (r.get("frmtrm_add_amount") or "").strip(),
            (r.get("bfefrmtrm_amount") or "").strip(),
        )
        try:
            ord_val = int(r.get("ord", "999") or 999)
        except (TypeError, ValueError):
            ord_val = 999
        existing = seen.get(key)
        if existing is None or ord_val < existing[0]:
            seen[key] = (ord_val, r)
    return [v[1] for v in seen.values()]


def _format_major_accounts(
    data: dict,
    *,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
) -> str:
    items = _dedup_account_rows(data.get("list") or [])
    title = f"# 주요계정 (corp_code={corp_code}, {bsns_year} {reprt_code_label(reprt_code)})"
    if not items:
        return title + "\n\n해당 연도/보고서의 주요계정 데이터가 없습니다."

    # fs_div(CFS/OFS) → sj_nm(재무제표명) → rows
    grouped: dict[str, dict[str, list[dict]]] = {}
    for r in items:
        fs = r.get("fs_div") or ""
        sj = r.get("sj_nm") or "(미분류)"
        grouped.setdefault(fs, {}).setdefault(sj, []).append(r)

    fs_label = {"CFS": "연결재무제표", "OFS": "별도재무제표"}
    lines = [title]

    # CFS를 먼저 보여주기 (대부분의 분석 표준)
    fs_order = sorted(grouped.keys(), key=lambda x: 0 if x == "CFS" else 1)

    for fs in fs_order:
        lines.append("")
        lines.append(f"## {fs_label.get(fs, fs)}")
        for sj, rows in grouped[fs].items():
            rows_sorted = sorted(
                rows,
                key=lambda r: (
                    _ACCOUNT_ORDER.get(r.get("account_nm", "").strip(), 999),
                    int(r.get("ord", "999") or 999),
                ),
            )
            cols, ambiguous = _period_columns(rows_sorted, reprt_code)
            lines.append("")
            lines.append(f"### {sj}")
            lines.extend(_render_amount_table(rows_sorted, cols))
            if ambiguous:
                lines.append("")
                lines.append(_CUM_MISSING_NOTE)

    # 통화 단위 안내
    currency = (items[0].get("currency") or "KRW").strip()
    lines.append("")
    lines.append(f"_통화: {currency}. 금액 단위는 DART 원본 그대로(보통 원). 회사별 표기 차이 가능._")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_major_accounts")
async def get_major_accounts(
    corp_code: str,
    bsns_year: int | str,
    reprt_code: str = "annual",
) -> str:
    """주요계정 — 정기보고서의 핵심 재무 (매출/영업이익/순이익/자산/부채/자본 등).

    "삼성전자 영업이익" 같은 흔한 질문에 가장 빠르게 답하는 도구. 사업보고서면
    3개년 비교, 분기/반기는 2개년.

    정정공시가 있으면 표 아래에 안내가 붙습니다 — 수치는 정정 반영본이지만
    사용자가 예전에 받아둔 값과 다를 수 있습니다.

    **분기/반기 손익은 3개월과 누적이 별도 컬럼으로 나옵니다** (예: 반기보고서 →
    `2분기(3개월)` / `상반기 누적` / `전년 2분기` / `전년 상반기`). "상반기 매출"을
    물었으면 누적 컬럼을 쓰세요 — 보고서가 반기라고 모든 숫자가 누적인 게 아닙니다.
    재무상태표는 시점 값이라 전기 컬럼이 전년 **말**(12/31)입니다.

    Args:
        corp_code: DART 8자리 고유번호. 모르면 search_company를 먼저.
        bsns_year: 사업연도 4자리 (예: 2024).
        reprt_code: "annual"(사업·기본), "Q1", "H1", "Q3" 또는 한글 라벨.

    Returns:
        연결재무제표(CFS) 우선, 손익→재무상태 순으로 정렬된 마크다운 표.
    """
    cc = normalize_corp_code(corp_code)
    yr = normalize_bsns_year(bsns_year)
    rc = normalize_reprt_code(reprt_code)
    data = await _fetch_major_accounts(cc, yr, rc)
    rows = data.get("list") or []
    correction, checked, err = (
        await _check_correction(cc, yr, rc) if rows else (None, True, None)
    )
    note = _correction_note(correction)
    scope = _financial_scope(rows)
    body = _format_major_accounts(data, corp_code=cc, bsns_year=yr, reprt_code=rc)
    warns: list[str] = []
    # 이 응답에 연결과 별도가 같이 들어 있으면, 표를 안 보고 행만 집계하는 쪽이
    # 둘을 더한다. 본문과 메타 양쪽에 적는다.
    if scope["scope_mixed_in_response"]:
        body = f"{body}\n\n{_SCOPE_MIX_NOTE}"
        warns.append(_SCOPE_MIX_NOTE)
    if note:
        body = f"{body}\n\n{note}"
        warns.append(note)
    if rows and not checked:
        warns.append(_correction_unchecked_note(err))
    return rmeta.append_meta(
        body,
        _dart_meta(
            rows=rows, corp_code=cc,
            data_period=f"{yr} {reprt_code_label(rc)}",
            data_completeness=rmeta.COMPLETE if rows else rmeta.NONE,
            extra={
                "financial_scope": scope,
                "filing_state": _filing_state(
                    bsns_year=yr, reprt_code=rc, rows=rows,
                    correction=correction, checked=checked,
                ),
            },
            warnings=warns or None,
        ),
    )


# ---------------------------------------------------------------------------
# get_full_financial (fnlttSinglAcntAll.json)
# ---------------------------------------------------------------------------

@cached(ttl_seconds=24 * 3600)
async def _fetch_full_financial(
    corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
) -> dict:
    try:
        return await get_json(
            "/fnlttSinglAcntAll.json",
            params={
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            },
        )
    except DartApiError as e:
        if e.status == "013":
            return {"status": "013", "message": e.message, "list": []}
        raise


_SJ_DIV_LABEL = {
    "BS": "재무상태표",
    "IS": "손익계산서",
    "CIS": "포괄손익계산서",
    "CF": "현금흐름표",
    "SCE": "자본변동표",
}


def _format_full_financial(
    data: dict,
    *,
    corp_code: str,
    bsns_year: str,
    reprt_code: str,
    fs_div: str,
    sj_div: str | None,
) -> str:
    items = data.get("list") or []
    fs_label = {"CFS": "연결", "OFS": "별도"}.get(fs_div, fs_div)
    title = (
        f"# 전체 재무제표 (corp_code={corp_code}, {bsns_year} "
        f"{reprt_code_label(reprt_code)} · {fs_label})"
    )
    if not items:
        return title + "\n\n해당 연도/보고서/구분의 데이터가 없습니다."

    if sj_div:
        items = [r for r in items if (r.get("sj_div") or "").strip() == sj_div]
        if not items:
            avail = sorted({r.get("sj_div") for r in (data.get("list") or []) if r.get("sj_div")})
            return (
                title
                + f"\n\nsj_div='{sj_div}' 결과 없음. "
                + f"사용 가능 구분: {', '.join(avail) or '(없음)'}"
            )

    # sj_div 안 줬을 때: 토큰 폭발 방지 — 각 sj_div의 행 수만 요약
    if not sj_div:
        by_sj: dict[str, int] = {}
        for r in items:
            by_sj[r.get("sj_div") or "?"] = by_sj.get(r.get("sj_div") or "?", 0) + 1
        lines = [
            title,
            "",
            f"전체 {len(items)}행. **sj_div를 지정해야 표를 반환합니다 (토큰 절약).**",
            "",
            "구분별 행 수:",
        ]
        for sj, cnt in sorted(by_sj.items()):
            label = _SJ_DIV_LABEL.get(sj, sj)
            lines.append(f"- `{sj}` {label}: {cnt}행")
        lines.append("")
        lines.append('재호출 예: `get_full_financial(corp_code, bsns_year, reprt_code, fs_div, sj_div="IS")`')
        return "\n".join(lines)

    # sj_div 지정 → 표 출력
    items_sorted = sorted(items, key=lambda r: int(r.get("ord", "999") or 999))
    cols, ambiguous = _period_columns(items_sorted, reprt_code)

    lines = [title, "", f"## {_SJ_DIV_LABEL.get(sj_div, sj_div)} ({len(items_sorted)}행)"]
    lines.extend(_render_amount_table(items_sorted, cols))
    if ambiguous:
        lines.append("")
        lines.append(_CUM_MISSING_NOTE)

    currency = (items[0].get("currency") or "KRW").strip()
    lines.append("")
    lines.append(f"_통화: {currency}. 1억 이상은 '억/조' 단위 압축, 미만은 콤마. 정확한 원 단위는 DART 원본 참조._")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_full_financial")
async def get_full_financial(
    corp_code: str,
    bsns_year: int | str,
    reprt_code: str = "annual",
    fs_div: str = "CFS",
    sj_div: str | None = None,
) -> str:
    """전체 재무제표 — 정기보고서의 모든 계정과목.

    행이 많으므로 (손익만 30~70행, 전체 200+) **반드시 sj_div로 한 표만 골라 호출**.
    sj_div를 비우면 구분별 행 수만 요약하고 표는 안 줌 (토큰 절약).

    분기/반기 손익(IS/CIS)은 `2분기(3개월)` / `상반기 누적`처럼 3개월과 누적이
    별도 컬럼으로 나옵니다. 컬럼명을 그대로 인용하세요.

    Args:
        corp_code: DART 8자리 고유번호.
        bsns_year: 사업연도 4자리.
        reprt_code: "annual" / "Q1" / "H1" / "Q3" 또는 한글.
        fs_div: "CFS"(연결, 기본) 또는 "OFS"(별도).
        sj_div: "BS"(재무상태표)/"IS"(손익)/"CIS"(포괄손익)/"CF"(현금흐름)/"SCE"(자본변동).
            None이면 표 대신 구분별 행 수만 반환.
    """
    cc = normalize_corp_code(corp_code)
    yr = normalize_bsns_year(bsns_year)
    rc = normalize_reprt_code(reprt_code)
    fs = normalize_fs_div(fs_div)
    sj = normalize_sj_div(sj_div)
    data = await _fetch_full_financial(cc, yr, rc, fs)
    rows = data.get("list") or []
    correction, checked, err = (
        await _check_correction(cc, yr, rc) if rows else (None, True, None)
    )
    note = _correction_note(correction)
    scope = _financial_scope(rows, requested_fs=fs)
    body = _format_full_financial(
        data, corp_code=cc, bsns_year=yr, reprt_code=rc, fs_div=fs, sj_div=sj
    )
    warns: list[str] = []
    if scope["scope_mixed_in_response"]:
        body = f"{body}\n\n{_SCOPE_MIX_NOTE}"
        warns.append(_SCOPE_MIX_NOTE)
    if note:
        body = f"{body}\n\n{note}"
        warns.append(note)
    if rows and not checked:
        warns.append(_correction_unchecked_note(err))
    return rmeta.append_meta(
        body,
        _dart_meta(
            rows=rows, corp_code=cc,
            data_period=f"{yr} {reprt_code_label(rc)} ({fs})",
            data_completeness=rmeta.COMPLETE if rows else rmeta.NONE,
            extra={
                "financial_scope": scope,
                "filing_state": _filing_state(
                    bsns_year=yr, reprt_code=rc, rows=rows,
                    correction=correction, checked=checked,
                ),
            },
            warnings=warns or None,
        ),
    )


# ---------------------------------------------------------------------------
# get_major_holders (majorstock.json) — 5%룰 대량보유 변동
# get_insider_trades (elestock.json) — 임원·주요주주 특정증권 소유
# ---------------------------------------------------------------------------

def _fmt_pct(value) -> str:
    """소수점 비율 포맷 — '12.34' → '12.34%'."""
    if value is None:
        return "-"
    s = str(value).strip()
    if not s or s == "-":
        return "-"
    return f"{s}%"


def _fmt_signed(value) -> str:
    """증감 컬럼 — 음수는 '-N', 양수는 '+N'."""
    s = _fmt_amount(value)
    if s in ("-", "0"):
        return s
    if s.startswith("-"):
        return s
    # _fmt_amount는 양수에 부호 안 붙임
    if s.replace(",", "").replace(".", "").isdigit():
        return "+" + s
    return s


@cached(ttl_seconds=5 * 60)
async def _fetch_major_holders(corp_code: str) -> dict:
    try:
        return await get_json("/majorstock.json", params={"corp_code": corp_code})
    except DartApiError as e:
        if e.status == "013":
            return {"status": "013", "message": e.message, "list": []}
        raise


@cached(ttl_seconds=5 * 60)
async def _fetch_insider_trades(corp_code: str) -> dict:
    try:
        return await get_json("/elestock.json", params={"corp_code": corp_code})
    except DartApiError as e:
        if e.status == "013":
            return {"status": "013", "message": e.message, "list": []}
        raise


def _format_major_holders(data: dict, *, corp_code: str, limit: int) -> str:
    items = data.get("list") or []
    title = f"# 대량보유(5%룰) 변동 (corp_code={corp_code}, 최근 {limit}건)"
    if not items:
        return title + "\n\n해당 회사의 대량보유 보고서가 없습니다."

    # 최신순 정렬 (rcept_dt + rcept_no desc) 후 limit
    items_sorted = sorted(
        items,
        key=lambda r: (r.get("rcept_dt", ""), r.get("rcept_no", "")),
        reverse=True,
    )[:limit]

    lines = [
        title,
        "",
        "| 접수일 | 보고자 | 보고유형 | 보유수 | 보유비율 | 증감수 | 증감비율 | 사유 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in items_sorted:
        d = r.get("rcept_dt", "")
        date_fmt = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        lines.append(
            "| {dt} | {who} | {tp} | {qty} | {rt} | {dq} | {dr} | {rsn} |".format(
                dt=date_fmt,
                who=(r.get("repror") or "-").replace("|", "·"),
                tp=(r.get("report_tp") or "-").replace("|", "·"),
                qty=_fmt_amount(r.get("stkqy")),
                rt=_fmt_pct(r.get("stkrt")),
                dq=_fmt_signed(r.get("stkqy_irds")),
                dr=_fmt_signed(r.get("stkrt_irds")),
                rsn=(
                    (r.get("report_resn") or "-")
                    .replace("|", "·")
                    .replace("\r", " ")
                    .replace("\n", " / ")
                )[:60],
            )
        )
    if len(items) > limit:
        lines.append("")
        lines.append(f"_표시 {limit}건 / 전체 {len(items)}건. limit 조정으로 더 보기 가능._")
    return "\n".join(lines)


def _format_insider_trades(data: dict, *, corp_code: str, limit: int) -> str:
    items = data.get("list") or []
    title = f"# 임원·주요주주 특정증권 소유 (corp_code={corp_code}, 최근 {limit}건)"
    if not items:
        return title + "\n\n해당 회사의 임원·주요주주 보고서가 없습니다."

    items_sorted = sorted(
        items,
        key=lambda r: (r.get("rcept_dt", ""), r.get("rcept_no", "")),
        reverse=True,
    )[:limit]

    lines = [
        title,
        "",
        "| 접수일 | 보고자 | 직위(등기/주요주주) | 소유수 | 소유비율 | 증감수 | 증감비율 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in items_sorted:
        d = r.get("rcept_dt", "")
        date_fmt = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        ofcps = (r.get("isu_exctv_ofcps") or "").strip()
        rgist = (r.get("isu_exctv_rgist_at") or "").strip()
        main = (r.get("isu_main_shrholdr") or "").strip()
        role_parts = [p for p in [ofcps, rgist, main] if p and p != "-"]
        role = " / ".join(role_parts) or "-"
        lines.append(
            "| {dt} | {who} | {role} | {qty} | {rt} | {dq} | {dr} |".format(
                dt=date_fmt,
                who=(r.get("repror") or "-").replace("|", "·"),
                role=role.replace("|", "·"),
                qty=_fmt_amount(r.get("sp_stock_lmp_cnt")),
                rt=_fmt_pct(r.get("sp_stock_lmp_rate")),
                dq=_fmt_signed(r.get("sp_stock_lmp_irds_cnt")),
                dr=_fmt_signed(r.get("sp_stock_lmp_irds_rate")),
            )
        )
    if len(items) > limit:
        lines.append("")
        lines.append(f"_표시 {limit}건 / 전체 {len(items)}건._")
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_major_holders")
async def get_major_holders(corp_code: str, limit: int = 10) -> str:
    """대량보유(5%룰) — 발행주식 5% 이상 보유자의 신규/변동/변경 보고서 목록.

    자본시장법 제147조에 따라 5% 이상 보유자(또는 1% 이상 변동)는 5영업일 내에
    DART에 보고해야 합니다. 외국인 펀드, 행동주의 투자자, 모회사 지분 변동 등
    **시세 데이터에는 안 보이는 자본 흐름**을 추적합니다.

    Args:
        corp_code: DART 8자리 고유번호. 모르면 search_company로 먼저.
        limit: 최신순 N건 (기본 10, 최대 50).

    Returns:
        접수일 / 보고자 / 보유수 / 비율 / 증감 / 사유 마크다운 표.
    """
    cc = normalize_corp_code(corp_code)
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        raise ValueError(f"limit은 1~50 사이의 정수여야 합니다 (받음: {limit}).")
    data = await _fetch_major_holders(cc)
    rows = data.get("list") or []
    return rmeta.append_meta(
        _format_major_holders(data, corp_code=cc, limit=limit),
        _dart_meta(rows=rows, corp_code=cc,
                   data_completeness=rmeta.COMPLETE if rows else rmeta.NONE),
    )


@mcp.tool()
@safe_tool
@track_metrics("get_insider_trades")
async def get_insider_trades(corp_code: str, limit: int = 10) -> str:
    """임원·주요주주 특정증권 소유 — 등기임원·주요주주(10% 이상)의 자사주 보유/매매.

    내부자가 자기 회사 주식을 사고팔면 5영업일 내에 보고해야 합니다(자본시장법
    제173조). **스마트머니 시그널** — CEO/CFO가 자기 회사 주식을 매수하면 펀더멘털에
    자신 있다는 신호로 자주 해석됩니다.

    Args:
        corp_code: DART 8자리 고유번호.
        limit: 최신순 N건 (기본 10, 최대 50).

    Returns:
        접수일 / 보고자 / 직위 / 소유수 / 비율 / 증감 마크다운 표.
    """
    cc = normalize_corp_code(corp_code)
    if not isinstance(limit, int) or limit < 1 or limit > 50:
        raise ValueError(f"limit은 1~50 사이의 정수여야 합니다 (받음: {limit}).")
    data = await _fetch_insider_trades(cc)
    rows = data.get("list") or []
    return rmeta.append_meta(
        _format_insider_trades(data, corp_code=cc, limit=limit),
        _dart_meta(rows=rows, corp_code=cc,
                   data_completeness=rmeta.COMPLETE if rows else rmeta.NONE),
    )


# ---------------------------------------------------------------------------
# get_disclosure_detail (document.xml)
# ---------------------------------------------------------------------------

import io
import re
import zipfile

from lxml import etree

# 짧은 공시 발췌 상한 (단순 본문 반환 시)
_SHORT_EXCERPT_CHARS = 3000
# 이 길이를 넘으면 '긴 보고서'로 간주 — 본문 대신 인덱스+find 안내 반환
_LONG_REPORT_THRESHOLD = 8000
# find 모드 — 매치당 주변 context
_FIND_CONTEXT_CHARS = 300
_FIND_MAX_MATCHES = 5


def _viewer_url(rcept_no: str) -> str:
    """DART 공시뷰어 URL — 사용자가 원문 전체를 보고 싶을 때."""
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def _extract_full_text(xml_bytes: bytes) -> str:
    """DART 공시 본문 XML에서 사람이 읽을 텍스트를 **전체** 추출 (cap 없음).

    호출처에서 필요한 만큼만 사용. XML 파싱 실패 시 정규식 fallback.
    """
    text: str = ""
    try:
        parser = etree.XMLParser(recover=True, huge_tree=True)
        root = etree.fromstring(xml_bytes, parser=parser)
        if root is not None:
            text = etree.tostring(root, method="text", encoding="unicode") or ""
    except Exception:
        text = ""

    if not text:
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                raw_str = xml_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raw_str = xml_bytes.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", raw_str)

    return re.sub(r"\s+", " ", text).strip()


@cached(ttl_seconds=24 * 3600)
async def _fetch_document_zip(rcept_no: str) -> bytes:
    return await get_bytes("/document.xml", params={"rcept_no": rcept_no})


def _parse_document_zip(raw: bytes) -> tuple[list[str], str]:
    """document.xml 응답 파싱. (파일목록, 본문 풀 텍스트) — cap 없음."""
    if raw[:2] != b"PK":
        return [], _extract_full_text(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if not names:
            return [], "(빈 zip)"
        xml_names = [n for n in names if n.lower().endswith(".xml")]
        target = xml_names[0] if xml_names else names[0]
        with zf.open(target) as fp:
            payload = fp.read()
    return names, _extract_full_text(payload)


def _guess_title(text: str) -> str:
    """본문 첫 100자 중 의미 있는 첫 라인 추정 — 보고서명 노출용."""
    head = text[:200].strip()
    # 공백 여러 개 앞에서 자르기 (DART 공시 본문은 종종 '사업보고서  N.N  회사명 ...' 형식)
    m = re.split(r"\s{2,}", head)
    if m and m[0]:
        return m[0][:80]
    return head[:80]


def _find_all_matches(text: str, keyword: str) -> list[int]:
    """키워드가 나오는 **모든** 위치. 스니펫은 만들지 않는다.

    세는 일과 보여주는 일을 나눈다. 5건만 만들어 놓고 "매치 5건"이라고 적으면
    17건 중 5건을 본 사람이 5건이 전부라고 읽는다. 그렇다고 전체에 스니펫을 다
    붙이면(1건당 600자) 흔한 단어 하나에 응답이 폭발한다. 위치는 전부 세고
    스니펫은 표시할 것만 만든다.
    """
    if not keyword:
        return []
    lower = text.lower()
    kw_lower = keyword.lower()
    out: list[int] = []
    pos = 0
    while True:
        idx = lower.find(kw_lower, pos)
        if idx < 0:
            return out
        out.append(idx)
        pos = idx + len(keyword)


def _find_matches(
    text: str, keyword: str, positions: list[int] | None = None
) -> list[dict]:
    """표시용 스니펫. positions 를 주면 그 위치들만, 안 주면 앞 5건."""
    if positions is None:
        positions = _find_all_matches(text, keyword)[:_FIND_MAX_MATCHES]
    results: list[dict] = []
    for idx in positions:
        start = max(0, idx - _FIND_CONTEXT_CHARS)
        end = min(len(text), idx + len(keyword) + _FIND_CONTEXT_CHARS)
        left = text[start:idx]
        match = text[idx:idx + len(keyword)]
        right = text[idx + len(keyword):end]
        snippet = f"{left}**{match}**{right}"
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        results.append({"pos": idx, "snippet": snippet})
    return results


def _format_short_disclosure(*, no: str, names: list[str], text: str) -> str:
    lines = [
        f"# 공시 본문 (rcept_no={no})",
        "",
        f"**원문 보기:** {_viewer_url(no)}",
    ]
    if names:
        lines.append("")
        lines.append(f"**zip 내 파일 ({len(names)}건):**")
        for n in names[:10]:
            lines.append(f"- {n}")
        if len(names) > 10:
            lines.append(f"- ... 외 {len(names) - 10}건")

    lines.append("")
    lines.append(f"## 본문 발췌 (전체 {len(text):,}자)")
    lines.append("")
    if len(text) <= _SHORT_EXCERPT_CHARS:
        lines.append(text or "(본문 텍스트를 추출하지 못했습니다. viewer URL에서 원문 확인.)")
    else:
        lines.append(text[:_SHORT_EXCERPT_CHARS])
        lines.append("")
        lines.append(f"_본문 {_SHORT_EXCERPT_CHARS}자까지 표시. 전체는 viewer URL._")
    return "\n".join(lines)


def _format_long_report(*, no: str, names: list[str], text: str) -> str:
    title = _guess_title(text)
    lines = [
        f"# 긴 보고서 (rcept_no={no})",
        "",
        f"**원문 보기:** {_viewer_url(no)}",
        "",
        f"**추정 제목:** {title}",
        f"**본문 길이:** {len(text):,}자 (긴 보고서 — 본문 발췌 생략)",
    ]
    if names:
        lines.append("")
        lines.append(f"**zip 내 파일 ({len(names)}건):**")
        for n in names[:10]:
            lines.append(f"- {n}")
        if len(names) > 10:
            lines.append(f"- ... 외 {len(names) - 10}건")

    lines.append("")
    lines.append("## 본문을 읽는 방법")
    lines.append("")
    lines.append("긴 정기보고서(사업/반기/분기/감사)는 본문이 수십 페이지라 토큰 절약을 위해 발췌하지 않습니다.")
    lines.append("")
    lines.append("- **특정 정보만 필요:** `get_disclosure_detail(rcept_no=..., find='키워드')` 로 재호출")
    lines.append("  - 매치 주변 ±300자, 최대 5건 발췌")
    lines.append("  - 예: `find='신사업'`, `find='배당'`, `find='주요 제품'`")
    lines.append("- **전체 본문 필요:** 위 viewer URL 클릭 (PDF/HTML 뷰어)")
    return "\n".join(lines)


def _format_find_results(
    *, no: str, keyword: str, matches: list[dict], total_len: int,
    total_matches: int | None = None,
) -> str:
    lines = [
        f"# 공시 본문 키워드 검색 (rcept_no={no})",
        "",
        f"**원문 보기:** {_viewer_url(no)}",
        f"**검색어:** `{keyword}`",
        f"**본문 전체 길이:** {total_len:,}자",
    ]
    if not matches:
        lines.append("")
        lines.append(f"'{keyword}' 매치 없음. 다른 키워드로 시도하거나 viewer URL에서 직접 확인하세요.")
        return "\n".join(lines)

    total = len(matches) if total_matches is None else total_matches
    # 라벨과 값이 어긋나면 안 된다. "매치 5건"은 실제 5건일 때만 맞다.
    if total > len(matches):
        lines.append(f"**매치:** 전체 {total}건 중 {len(matches)}건 표시")
    else:
        lines.append(f"**매치:** {total}건 (최대 {_FIND_MAX_MATCHES}건 표시)")
    for i, m in enumerate(matches, 1):
        lines.append("")
        lines.append(f"## 매치 {i} (위치 ~{m['pos']:,})")
        lines.append("")
        lines.append(m["snippet"])
    if total > len(matches):
        lines.append("")
        lines.append(
            f"_나머지 {total - len(matches)}건은 표시되지 않았습니다. "
            "더 좁은 키워드로 다시 찾거나 viewer URL에서 확인하세요._"
        )
    return "\n".join(lines)


@mcp.tool()
@safe_tool
@track_metrics("get_disclosure_detail")
async def get_disclosure_detail(rcept_no: str, find: str | None = None) -> str:
    """공시본문 — rcept_no로 공시 원문 조회. 보고서 길이에 따라 자동 분기.

    - find 인자 있음: 키워드 검색 모드 — 본문에서 키워드 주변 발췌 최대 5건. 긴 보고서에서 특정 정보만 추출.
      예: find="신사업", find="배당금"
    - 짧은 공시: 본문 발췌 + viewer URL (대량보유·임원매매·단일계약 등)
    - 긴 보고서(사업/반기/분기/감사): 본문 생략, 제목+파일목록+viewer URL. 전체를 토큰에 박으면 낭비라 find로 좁혀 쓰기.

    Args:
        rcept_no: DART 공시 접수번호 14자리 (list_disclosures 결과에서 얻음).
        find: 본문 검색 키워드 (선택). 지정 시 키워드 검색 모드.
    """
    no = normalize_rcept_no(rcept_no)
    raw = await _fetch_document_zip(no)
    names, text = _parse_document_zip(raw)

    if find and find.strip():
        kw = find.strip()
        positions = _find_all_matches(text, kw)
        matches = _find_matches(text, kw, positions[:_FIND_MAX_MATCHES])
        total_matches = len(positions)
        truncated = total_matches > len(matches)
        # 0건은 "본문에 없다"가 아니라 "이 키워드로는 못 찾았다"이다. 표 안
        # 텍스트나 다른 표기(현금배당 등)면 실제로 있어도 0건이 나온다.
        # 잘렸어도 다 본 게 아니다. 둘 다 complete 라고 말할 수 없다.
        match_coverage = {
            "keyword": kw,
            "total_matches": total_matches,
            "displayed_matches": len(matches),
            "truncated": truncated,
            "coverage_complete": bool(matches) and not truncated,
        }
        extra = {"match_coverage": match_coverage}
        if total_matches == 0:
            extra["absence_confirmed"] = False
        return rmeta.append_meta(
            _format_find_results(
                no=no, keyword=kw, matches=matches, total_len=len(text),
                total_matches=total_matches,
            ),
            _dart_meta(
                rcept_no=no,
                data_completeness=(rmeta.COMPLETE
                                   if match_coverage["coverage_complete"]
                                   else rmeta.PARTIAL),
                coverage=None,
                extra=extra,
                warnings=None if matches else [
                    f"'{find.strip()}' 매치 0건 — 본문에 해당 내용이 **없다는 뜻이 아니다**. "
                    "표기가 다르거나 표 안에 있어 텍스트 추출에서 빠졌을 수 있으니 "
                    "부정 결론을 내리지 말고 다른 키워드나 viewer URL로 확인하라."
                ],
            ),
        )

    if len(text) > _LONG_REPORT_THRESHOLD:
        # 본문을 안 준 상태다. 학습지식으로 메우면 안 된다는 사실을 메타로 못 박는다.
        return rmeta.append_meta(
            _format_long_report(no=no, names=names, text=text),
            _dart_meta(
                rcept_no=no,
                data_completeness=rmeta.PARTIAL,
                warnings=[
                    "긴 보고서라 본문을 반환하지 않았다. 내용을 물으면 find= 로 다시 "
                    "조회하라. 본문 없이 학습지식으로 답하지 마라."
                ],
            ),
        )
    return rmeta.append_meta(
        _format_short_disclosure(no=no, names=names, text=text),
        _dart_meta(rcept_no=no),
    )


# ---------------------------------------------------------------------------
# get_order_backlog — DART report table parser
# ---------------------------------------------------------------------------

_ORDER_BACKLOG_SCAN_LIMIT = 50
_ORDER_BACKLOG_MAX_REPORTS = 10


def _order_backlog_report_candidates(items: list[dict]) -> list[dict]:
    reports = [item for item in items if (item.get("rcept_no") or "").strip()]
    return sorted(reports, key=_order_backlog_report_sort_key)


def _order_backlog_report_sort_key(item: dict) -> tuple[int, int]:
    report_name = item.get("report_nm") or ""
    rcept_dt = item.get("rcept_dt") or "0"
    if "사업보고서" in report_name:
        priority = 0
    elif "반기보고서" in report_name:
        priority = 1
    elif "분기보고서" in report_name:
        priority = 2
    else:
        priority = 3
    try:
        date_key = -int(rcept_dt)
    except ValueError:
        date_key = 0
    return priority, date_key


def _order_backlog_report_name(item: dict) -> str:
    return str(item.get("report_nm") or "정기보고서").replace("|", "·")


def _order_backlog_report_period(item: dict) -> str | None:
    report_name = item.get("report_nm") or ""
    match = re.search(r"(20\d{2})(?:[.년/-]?\s*(?:12|06|03|09))?", report_name)
    if match:
        return match.group(1)
    rcept_dt = item.get("rcept_dt") or ""
    if len(rcept_dt) >= 4 and rcept_dt[:4].isdigit():
        return str(int(rcept_dt[:4]) - 1)
    return None


@mcp.tool()
@safe_tool
@track_metrics("get_order_backlog")
async def get_order_backlog(corp_code: str, years: int = 3, days: int = 1200) -> str:
    """수주잔고 추이 — 사업/반기/분기보고서 원문 표에서 수주잔고·계약잔액을 구조화.

    이 도구는 증권사 리포트가 아니라 DART 원문을 1차 출처로 사용합니다. 조선·방산·건설·장비주처럼
    수주잔고가 중요한 업종에서 유용하며, 표 구조가 회사마다 달라 추정이 필요한 값은 건너뜁니다.

    Args:
        corp_code: DART 8자리 고유번호.
        years: 반환할 최근 기간 수. 1~5.
        days: 최근 정기보고서 검색 범위. 기본 1200일, 최대 3650일.

    Returns:
        그래프화하기 쉬운 수주잔고 시계열 텍스트와 DART rcept_no 출처.
    """
    cc = normalize_corp_code(corp_code)
    if not isinstance(years, int) or years < 1 or years > 5:
        raise ValueError(f"years는 1~5 사이의 정수여야 합니다 (받음: {years}).")

    bgn, end = days_to_range(days)
    data = await _fetch_disclosure_list(cc, bgn, end, "A", _ORDER_BACKLOG_SCAN_LIMIT)
    candidates = _order_backlog_report_candidates(data.get("list") or [])
    if not candidates:
        return f"# 수주잔고 추이 (corp_code={cc})\n\n최근 {days}일 정기보고서를 찾지 못했습니다."

    attempted: list[str] = []
    yearly_points = []
    sources: list[str] = []
    seen_periods: set[str] = set()
    for report in candidates[:_ORDER_BACKLOG_MAX_REPORTS]:
        rcept_no = normalize_rcept_no(str(report.get("rcept_no") or ""))
        report_name = _order_backlog_report_name(report)
        attempted.append(f"{report_name} rcept_no={rcept_no}")
        raw = await _fetch_document_zip(rcept_no)
        tables = extract_document_tables(raw)
        series = extract_order_backlog_series(tables, limit=years)
        if series is not None and len(series.points) >= 2:
            return format_order_backlog_series(
                corp_code=cc,
                report_name=report_name,
                rcept_no=rcept_no,
                series=series,
            )
        period = _order_backlog_report_period(report)
        if period is None or period in seen_periods:
            continue
        point = extract_order_backlog_point(tables, period=period)
        if point is None:
            continue
        seen_periods.add(period)
        yearly_points.append(point)
        sources.append(f"{period}: {report_name} rcept_no={rcept_no}")
        if len(yearly_points) >= years:
            break

    if yearly_points:
        yearly_points = sorted(yearly_points, key=lambda point: point.period)[-years:]
        return format_order_backlog_series(
            corp_code=cc,
            report_name="복수 정기보고서",
            rcept_no=", ".join(point.period for point in yearly_points),
            series=OrderBacklogSeries(metric="수주잔고", unit="억원", points=yearly_points),
            sources=sources[-len(yearly_points):],
        )

    lines = [
        f"# 수주잔고 추이 (corp_code={cc})",
        "",
        "정기보고서에서 구조화 가능한 수주잔고 표를 찾지 못했습니다.",
        "",
        "확인한 보고서:",
    ]
    lines.extend(f"- {item}" for item in attempted)
    lines.extend(
        [
            "",
            "원문 키워드 검색으로 확인하려면 `get_disclosure_detail(rcept_no=..., find='수주잔고')` 또는",
            "`find='계약잔액'`, `find='수주상황'`을 사용하세요.",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# scan_earnings_season (fnlttMultiAcnt.json) — 어닝 시즌 일괄 스캐닝
# ---------------------------------------------------------------------------

@mcp.tool()
@safe_tool
@track_metrics("scan_earnings_season")
async def scan_earnings_season(
    period: str,
    universe: str = "kospi",
    sort_by: str = "op_yoy",
    direction: str = "desc",
    top_n: int = 30,
    fs_div: str = "CFS",
    group_by: str | None = None,
) -> str:
    """분기/연간 실적 스캐닝 — 유니버스 전체의 당기·전년동기 핵심계정을 일괄 조회해 YoY 계산·정렬 후 top_n을 마크다운 표로 반환.

    "이번 분기 실적 다 뒤져서 두드러진 회사 추려줘"를 한 번에 처리. get_major_accounts/get_full_financial
    단건 fan-out 대신 사용. 결과의 corp_code로 후속 단건 도구 호출. 각 행 공시일은 회사별 DART 접수일 —
    주가·수급 반응은 그 공시일을 StockLens event_date로 넘김(마감일 일괄 아님).

    Args:
        period: "YYYYQ1"/"YYYYH1"/"YYYYQ3"/"YYYY"(연간). Q2/Q4/H2는 정기보고서 없어 미지원.
        universe: "all"/"kospi"/"kosdaq" 또는 corp_code 콤마 리스트. 기본 kospi.
        sort_by: rev_yoy/op_yoy/ni_yoy(YoY %), op_margin, rev/op/ni(절댓값). 기본 op_yoy.
        direction: desc/asc. 결측(N/A)은 항상 맨 뒤.
        top_n: 1~100, 기본 30. (group_by="sector"면 섹터 개수 상한)
        fs_div: "CFS"(연결, 기본)/"OFS"(별도).
        group_by: None이면 종목별 표. "sector"면 KSIC 업종별 집계(회사수·영익증가비율·흑전비율·
                YoY 중앙값, 영익증가비율 desc). 테마(원전·AI반도체 등)는 KSIC로 안 잡힘.
    """
    return await run_scan(
        period=period,
        universe=universe,
        sort_by=sort_by,
        direction=direction,
        top_n=top_n,
        fs_div=fs_div,
        group_by=group_by,
    )


@mcp.tool()
@safe_tool
@track_metrics("export_earnings_scan")
async def export_earnings_scan(
    period: str,
    universe: str = "kospi",
    sort_by: str = "op_yoy",
    direction: str = "desc",
    max_rows: int = 1000,
    output_format: str = "xlsx",
    amount_unit: str = "eok",
    fs_div: str = "CFS",
) -> str:
    """실적 스캔 파일 생성 — scan_earnings_season 결과를 스프레드시트 파일로 저장.

    한국어 Excel의 CSV 구분자 문제로 쉼표 포함 텍스트가 열 밀림을 만들 수 있어 기본값은 XLSX.

    Args:
        period: "YYYYQ1"/"YYYYH1"/"YYYYQ3"/"YYYY".
        universe: "all"/"kospi"/"kosdaq" 또는 corp_code 콤마 리스트.
        sort_by: rev_yoy/op_yoy/ni_yoy/op_margin/rev/op/ni.
        direction: desc/asc.
        max_rows: 저장 최대 행 수. 기본 1000, 최대 3000.
        output_format: "xlsx"(기본)/"csv"/"both".
        amount_unit: "eok"(억원, 기본)/"won"(원).
        fs_div: "CFS"(연결, 기본)/"OFS"(별도).
    """
    result = await run_export(
        period=period,
        universe=universe,
        sort_by=sort_by,
        direction=direction,
        max_rows=max_rows,
        output_format=output_format,
        amount_unit=amount_unit,
        fs_div=fs_div,
    )
    return result.to_markdown()


# ---------------------------------------------------------------------------
# dartlens_status — 자가진단 (라이선스 게이트 의도적 미적용, 아래 참고)
# ---------------------------------------------------------------------------

def _format_status(
    *,
    version: str,
    latest_version: str | None,
    license_diag,
    api_diag,
    cache_diag: dict,
    call_status: dict,
    checked_online: bool,
) -> str:
    lines = ["# DartLens 상태", ""]

    update_line = f"- 버전: {version}"
    if latest_version and latest_version != version:
        # 터미널 명령을 안내하면 주 고객층은 거기서 막힌다 — LeetKit Manager가 있는
        # 이유가 그 명령을 안 치게 하려는 것이다. 세 Lens 안내를 같은 말로 맞춘다.
        update_line += f" (최신: {latest_version} — LeetKit Manager를 열고 [지금 업데이트])"
    elif latest_version:
        update_line += " (최신 버전)"
    else:
        update_line += " (최신 버전 확인 불가 — PyPI 연결 실패)"
    lines.append(update_line)

    if license_diag.status == "active":
        lines.append(f"- 라이선스: 활성화 (ID: {license_diag.license_id_masked})")
    else:
        lines.append(f"- ⚠️ 라이선스: {license_diag.message}")

    api_note = " (실제 유효성 확인됨)" if checked_online else " (형식만 확인 — 실제 유효성은 check_online=True)"
    if api_diag.status == "valid":
        lines.append(f"- DART API 키: 정상{api_note} — {api_diag.storage}, {api_diag.key_tail_masked}")
    elif api_diag.status in ("rate_limited", "network_unreachable"):
        lines.append(f"- DART API 키: {api_diag.message} (일시적 문제로 보임)")
    else:
        lines.append(f"- ⚠️ DART API 키: {api_diag.message}")

    if call_status["last_call_at"]:
        lines.append(f"- 최근 DART 호출: {call_status['last_call_at']} (status {call_status['last_status']})")
    else:
        lines.append("- 최근 DART 호출: 기록 없음")
    if call_status["last_success_at"]:
        lines.append(f"- 최근 성공 호출: {call_status['last_success_at']}")

    if cache_diag["exists"]:
        freshness = "최신" if cache_diag["is_fresh"] else "오래됨(TTL 초과)"
        entries = f"{cache_diag['entry_count']:,}개 기업" if cache_diag["entry_count"] is not None else "파싱 실패"
        lines.append(f"- corp code 캐시: {entries}, {cache_diag['last_updated']} 갱신 ({freshness})")
    else:
        lines.append("- corp code 캐시: 없음 (첫 조회 시 자동 다운로드)")

    return "\n".join(lines)


@mcp.tool()
@track_metrics("dartlens_status")
async def dartlens_status(check_online: bool = False) -> str:
    """DartLens 자가진단 — 패키지 버전·라이선스·DART API 키·최근 DART 호출·corp code 캐시·업데이트 가능 여부를 한 번에 확인.

    라이선스나 API 키가 없어도 동작한다(문제 원인을 보여주는 게 목적이라 다른 도구처럼
    라이선스 게이트를 걸지 않음 — 재무 데이터는 전혀 포함하지 않아 게이트가 필요 없음).
    "DartLens가 왜 안 되지" 류 질문에서 dartlens-doctor 안내 전에 먼저 호출하기 좋음.

    Args:
        check_online: True면 DART에 가벼운 엔드포인트 1회 호출해 API 키의 실제 유효성까지 확인.
            기본 False(형식/저장소만 확인, 네트워크 호출 없음 — 빠름).
    """
    try:
        license_diag = diagnostics.diagnose_license()
        api_diag = (
            await diagnostics.diagnose_dart_api_key_online()
            if check_online
            else diagnostics.diagnose_dart_api_key()
        )
        cache_diag = cache_diagnosis()
        call_status = read_dart_call_status()
        latest_version = await diagnostics.fetch_latest_pypi_version()

        import dartlens as _dartlens_pkg

        return _format_status(
            version=_dartlens_pkg.__version__,
            latest_version=latest_version,
            license_diag=license_diag,
            api_diag=api_diag,
            cache_diag=cache_diag,
            call_status=call_status,
            checked_online=check_online,
        )
    except Exception as e:
        return f"⚠️ 상태 조회 중 오류: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """`dartlens` 진입점 — stdio MCP 서버 실행."""
    mcp.run()


if __name__ == "__main__":
    main()
