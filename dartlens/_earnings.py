"""scan_earnings_season 핵심 로직 — period 파싱 / universe 해석 /
다중회사 병렬 fetch / YoY 계산 / 정렬 / 마크다운 포매팅.

server.py는 이 모듈의 run_scan을 얇게 감싸기만 한다 (_order_backlog.py와 동일 패턴).
순수 함수(parse_period / extract_accounts / compute_row / sort)는 테스트에서
네트워크 없이 직접 검증한다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from dartlens._cache import EarningsCache, get_earnings_cache
from dartlens._corp_code import all_listed, corp_name_map
from dartlens._http import get_multi_acnt
from dartlens._safe import DartApiError
from dartlens._validate import normalize_bsns_year, normalize_corp_code

# ---------------------------------------------------------------------------
# period 파싱
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"^(\d{4})(Q[1-4]|H[12])?$", re.IGNORECASE)

# 접미사 → DART reprt_code. Q2/Q4/H2는 정기보고서가 없어 v1 미지원.
_PERIOD_REPRT = {
    "": "11011",     # 사업보고서 (연간)
    "Q1": "11013",   # 1분기보고서
    "H1": "11012",   # 반기보고서
    "Q3": "11014",   # 3분기보고서
}


def parse_period(period: str) -> tuple[int, str]:
    """"2026Q1" → (2026, "11013"), "2025H1" → (2025, "11012"),
    "2024" → (2024, "11011"). Q2/Q4/H2 → ValueError (v2 잠정실적과 함께).
    """
    if not period or not str(period).strip():
        raise ValueError("period가 비어있습니다 (예: 2026Q1 / 2025H1 / 2024).")
    m = _PERIOD_RE.match(str(period).strip())
    if not m:
        raise ValueError(
            f"period 형식 오류: '{period}'. "
            "YYYY / YYYYQ1 / YYYYQ3 / YYYYH1 형식이어야 합니다."
        )
    year = int(normalize_bsns_year(m.group(1)))
    suffix = (m.group(2) or "").upper()
    if suffix not in _PERIOD_REPRT:
        raise ValueError(
            f"'{suffix}'는 정기보고서가 없어 v1에서 지원하지 않습니다 "
            "(Q4/H2/Q2는 v2에서 잠정실적공시와 함께 지원). "
            "사용 가능: YYYY(연간) / YYYYQ1 / YYYYH1 / YYYYQ3."
        )
    return year, _PERIOD_REPRT[suffix]


def period_label(year: int, reprt_code: str) -> str:
    suffix = {"11011": "연간", "11013": "Q1", "11012": "H1", "11014": "Q3"}.get(
        reprt_code, reprt_code
    )
    return f"{year}{'' if suffix == '연간' else suffix}" + (
        " (연간)" if suffix == "연간" else ""
    )


# ---------------------------------------------------------------------------
# universe 해석
# ---------------------------------------------------------------------------


async def resolve_universe(universe: str) -> tuple[list[str], str | None]:
    """universe 문자열 → (corp_code 리스트, 경고 메시지|None).

    "all"/"kospi"/"kosdaq" → 상장사 전체 (corpCode.xml에 시장 구분이 없어
    kospi/kosdaq는 전체 폴백 + 경고). 콤마 리스트 또는 단일 corp_code → 그대로.
    """
    u = (universe or "").strip()
    if not u:
        raise ValueError("universe가 비어있습니다 (all/kospi/kosdaq 또는 corp_code 리스트).")

    low = u.lower()
    if low in ("all", "kospi", "kosdaq"):
        entries = await all_listed()
        codes = sorted({e.corp_code for e in entries if e.is_listed})
        if not codes:
            raise ValueError("corpCode.xml에서 상장사를 찾지 못했습니다 (캐시 손상?).")
        warn = None
        if low in ("kospi", "kosdaq"):
            warn = (
                f"corpCode.xml에 시장 구분이 없어 '{low}' 요청을 전체 상장사 "
                f"{len(codes)}개로 폴백했습니다 (v2에서 KRX 매핑 예정)."
            )
        return codes, warn

    # corp_code 콤마 리스트 (단일도 허용)
    raw = [c.strip() for c in u.split(",") if c.strip()]
    if not raw:
        raise ValueError(f"universe 파싱 결과가 비었습니다: '{universe}'.")
    codes = [normalize_corp_code(c) for c in raw]
    # 중복 제거 (입력 순서 보존)
    seen: set[str] = set()
    deduped = [c for c in codes if not (c in seen or seen.add(c))]
    return deduped, None


# ---------------------------------------------------------------------------
# 금액 파싱 / 포매팅
# ---------------------------------------------------------------------------


def parse_won(value) -> float | None:
    """DART 금액 문자열 → float. 콤마/괄호 음수/공백/'-' 결측 처리."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ("-", "—"):
        return None
    sign = 1.0
    if s.startswith("-"):
        sign, s = -1.0, s[1:]
    elif s.startswith("(") and s.endswith(")"):
        sign, s = -1.0, s[1:-1]
    s = s.replace(",", "").strip()
    try:
        return sign * float(s)
    except ValueError:
        return None


def fmt_won(value: float | None) -> str:
    """원 단위 압축 — '조 억'. _format_major_accounts와 시각적 일관성 유지.

    server._fmt_won 재사용은 순환 import(server→_earnings) 때문에 불가하므로
    동일 규칙을 여기서 독립 구현한다.
    """
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    n = int(abs(value))
    JO = 1_000_000_000_000
    EOK = 100_000_000
    if n >= JO:
        jo, eok = n // JO, (n % JO) // EOK
        return f"{sign}{jo:,}조 {eok:,}억" if eok else f"{sign}{jo:,}조"
    if n >= EOK:
        return f"{sign}{n // EOK:,}억"
    return f"{sign}{n:,}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{'+' if value >= 0 else ''}{value:.1f}%"


# ---------------------------------------------------------------------------
# 계정 추출
# ---------------------------------------------------------------------------

# DART fnlttMultiAcnt account_nm은 회사·연도별로 표기가 갈린다.
_REV_NAMES = {"매출액", "매출", "수익(매출액)", "영업수익", "영업수익(매출액)"}
_OP_NAMES = {"영업이익", "영업이익(손실)", "영업손실"}
_NI_NAMES = {
    "당기순이익",
    "당기순이익(손실)",
    "당기순손실",
    "분기순이익",
    "분기순이익(손실)",
    "반기순이익",
    "반기순이익(손실)",
    "당기순이익(당기손실)",
}


def extract_accounts(
    rows: list[dict], corp_code: str, fs_div: str
) -> dict | None:
    """한 회사의 fnlttMultiAcnt 행들에서 (매출/영업이익/순이익) 당기·전기 추출.

    fs_div 일치 행만 사용. 같은 계정이 IS/CIS 양쪽에 있으면 먼저 본 값 우선.
    핵심 세 계정이 모두 결측이면 None (데이터 미보유로 간주, 캐시 안 함).
    반환: {corp_name, rev_cur, rev_prev, op_cur, op_prev, ni_cur, ni_prev}
    """
    corp_rows = [
        r
        for r in rows
        if (r.get("corp_code") or "").strip() == corp_code
        and (r.get("fs_div") or "").strip() == fs_div
    ]
    if not corp_rows:
        return None

    corp_name = (corp_rows[0].get("corp_name") or "").strip()
    picked: dict[str, dict] = {}  # bucket → row (먼저 본 것 우선)

    for r in corp_rows:
        nm = (r.get("account_nm") or "").strip()
        bucket = None
        if nm in _REV_NAMES:
            bucket = "rev"
        elif nm in _OP_NAMES:
            bucket = "op"
        elif nm in _NI_NAMES:
            bucket = "ni"
        if bucket and bucket not in picked:
            picked[bucket] = r

    if not picked:
        return None

    out: dict = {"corp_name": corp_name}
    for bucket in ("rev", "op", "ni"):
        r = picked.get(bucket)
        out[f"{bucket}_cur"] = parse_won(r.get("thstrm_amount")) if r else None
        out[f"{bucket}_prev"] = parse_won(r.get("frmtrm_amount")) if r else None
    return out


# ---------------------------------------------------------------------------
# YoY / 행 계산
# ---------------------------------------------------------------------------


@dataclass
class ScanRow:
    corp_code: str
    corp_name: str
    rev: float | None
    rev_yoy: float | None
    op: float | None
    op_yoy: float | None
    ni: float | None
    ni_yoy: float | None
    op_margin: float | None
    note: str


def _yoy(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def compute_row(corp_code: str, acc: dict) -> ScanRow:
    """추출 dict → ScanRow. 흑전/적전 비고 한글 표기."""
    rev, op, ni = acc.get("rev_cur"), acc.get("op_cur"), acc.get("ni_cur")
    op_prev, ni_prev = acc.get("op_prev"), acc.get("ni_prev")
    op_margin = (op / rev * 100.0) if (op is not None and rev) else None

    notes: list[str] = []
    # 영업이익 기준 흑전/적전 (가장 관심도 높은 시그널)
    if op is not None and op_prev is not None:
        if op_prev <= 0 < op:
            notes.append("영업 흑전")
        elif op_prev > 0 >= op:
            notes.append("영업 적전")
    if ni is not None and ni_prev is not None:
        if ni_prev <= 0 < ni:
            notes.append("순익 흑전")
        elif ni_prev > 0 >= ni:
            notes.append("순익 적전")

    return ScanRow(
        corp_code=corp_code,
        corp_name=acc.get("corp_name") or "",
        rev=rev,
        rev_yoy=_yoy(rev, acc.get("rev_prev")),
        op=op,
        op_yoy=_yoy(op, op_prev),
        ni=ni,
        ni_yoy=_yoy(ni, ni_prev),
        op_margin=op_margin,
        note=", ".join(notes) if notes else "-",
    )


_SORT_KEYS = {
    "rev_yoy": "rev_yoy",
    "op_yoy": "op_yoy",
    "ni_yoy": "ni_yoy",
    "op_margin": "op_margin",
    "rev": "rev",
    "op": "op",
    "ni": "ni",
}


def sort_rows(
    rows: list[ScanRow], sort_by: str, direction: str
) -> list[ScanRow]:
    """sort_by/direction 적용. None(결측)은 방향 무관 항상 맨 뒤."""
    if sort_by not in _SORT_KEYS:
        raise ValueError(
            f"sort_by '{sort_by}' 미지원. 사용 가능: {', '.join(_SORT_KEYS)}."
        )
    if direction not in ("desc", "asc"):
        raise ValueError(f"direction은 desc 또는 asc여야 합니다 (받음: '{direction}').")

    attr = _SORT_KEYS[sort_by]
    reverse = direction == "desc"

    def key(r: ScanRow):
        v = getattr(r, attr)
        # (결측 플래그, 값) — 결측은 항상 뒤. asc/desc 모두에서 뒤로 가도록
        # reverse 적용 후를 고려해 부호를 맞춘다.
        if v is None:
            return (1, 0.0) if not reverse else (-1, 0.0)
        return (0, v) if not reverse else (0, v)

    return sorted(rows, key=key, reverse=reverse)


# ---------------------------------------------------------------------------
# 다중회사 병렬 fetch (캐시 우선)
# ---------------------------------------------------------------------------

_CHUNK = 100  # DART fnlttMultiAcnt 회사 한도
_MAX_CONCURRENT_CHUNKS = 5


async def _fetch_year(
    corp_codes: list[str],
    year: int,
    reprt_code: str,
    fs_div: str,
    cache: EarningsCache,
) -> tuple[dict[str, dict], int, int, int]:
    """특정 (year, reprt, fs_div)에 대해 캐시 미스 corp만 chunk fetch.

    반환: (corp_code→accounts, cache_hits, api_fetched, api_call_count).
    캐시 hit corp는 API 스킵. 데이터 있는 corp만 캐시에 저장(미접수분 재시도).
    """
    keys = {
        cc: EarningsCache.make_key(cc, year, reprt_code, fs_div)
        for cc in corp_codes
    }
    cached = await asyncio.to_thread(cache.get_many, list(keys.values()))

    result: dict[str, dict] = {}
    misses: list[str] = []
    for cc, k in keys.items():
        if k in cached:
            result[cc] = cached[k]
        else:
            misses.append(cc)

    cache_hits = len(result)
    chunks = [misses[i : i + _CHUNK] for i in range(0, len(misses), _CHUNK)]
    sem = asyncio.Semaphore(_MAX_CONCURRENT_CHUNKS)
    api_calls = 0
    failed_chunks = 0

    async def run_chunk(chunk: list[str]) -> dict[str, dict]:
        nonlocal api_calls, failed_chunks
        async with sem:
            try:
                rows = await get_multi_acnt(chunk, year, reprt_code)
            except DartApiError:
                failed_chunks += 1
                return {}
            api_calls += 1
            out: dict[str, dict] = {}
            for cc in chunk:
                acc = extract_accounts(rows, cc, fs_div)
                if acc is not None:
                    out[cc] = acc
            return out

    chunk_results = await asyncio.gather(*(run_chunk(c) for c in chunks))

    fetched: dict[str, dict] = {}
    for cr in chunk_results:
        fetched.update(cr)

    if fetched:
        to_store = {
            EarningsCache.make_key(cc, year, reprt_code, fs_div): acc
            for cc, acc in fetched.items()
        }
        await asyncio.to_thread(cache.set_many, to_store)

    result.update(fetched)
    return result, cache_hits, len(fetched), api_calls + failed_chunks


# ---------------------------------------------------------------------------
# 메인 진입점
# ---------------------------------------------------------------------------


async def run_scan(
    *,
    period: str,
    universe: str = "kospi",
    sort_by: str = "op_yoy",
    direction: str = "desc",
    top_n: int = 30,
    fs_div: str = "CFS",
    cache: EarningsCache | None = None,
) -> str:
    """scan_earnings_season 본체. 마크다운 str 반환."""
    year, reprt_code = parse_period(period)

    if fs_div not in ("CFS", "OFS"):
        raise ValueError(f"fs_div는 CFS 또는 OFS여야 합니다 (받음: '{fs_div}').")
    if not isinstance(top_n, int) or not (1 <= top_n <= 100):
        raise ValueError(f"top_n은 1~100 정수여야 합니다 (받음: {top_n}).")
    if sort_by not in _SORT_KEYS:
        raise ValueError(
            f"sort_by '{sort_by}' 미지원. 사용 가능: {', '.join(_SORT_KEYS)}."
        )
    if direction not in ("desc", "asc"):
        raise ValueError(f"direction은 desc 또는 asc여야 합니다 (받음: '{direction}').")

    corp_codes, universe_warn = await resolve_universe(universe)
    universe_size = len(corp_codes)
    big_universe_warn = None
    if universe_size > 2000:
        big_universe_warn = (
            f"유니버스가 {universe_size}개로 큽니다 — 1차 스캔에 시간이 더 걸릴 수 있습니다."
        )

    if cache is None:
        cache = get_earnings_cache()

    # 당기 + 전년동기 (전년 frmtrm 결측 보완용) — 두 세트 병렬
    (cur_map, cur_hits, cur_fetched, cur_calls), (
        prev_map,
        prev_hits,
        prev_fetched,
        prev_calls,
    ) = await asyncio.gather(
        _fetch_year(corp_codes, year, reprt_code, fs_div, cache),
        _fetch_year(corp_codes, year - 1, reprt_code, fs_div, cache),
    )

    rows: list[ScanRow] = []
    for cc in corp_codes:
        acc = cur_map.get(cc)
        if acc is None:
            continue
        acc = dict(acc)  # 캐시 dict 변형 방지
        # frmtrm 결측 시 전년도 호출의 thstrm로 보완
        for bucket in ("rev", "op", "ni"):
            if acc.get(f"{bucket}_prev") is None:
                prev_acc = prev_map.get(cc)
                if prev_acc is not None:
                    acc[f"{bucket}_prev"] = prev_acc.get(f"{bucket}_cur")
        rows.append(compute_row(cc, acc))

    # fnlttMultiAcnt 응답에는 corp_name이 없다 → corpCode.xml에서 일괄 해석
    name_map = await corp_name_map([r.corp_code for r in rows])
    for r in rows:
        if not r.corp_name:
            r.corp_name = name_map.get(r.corp_code, r.corp_code)

    data_count = len(rows)
    sorted_rows = sort_rows(rows, sort_by, direction)
    top = sorted_rows[:top_n]

    missing = universe_size - data_count
    total_api = cur_calls + prev_calls
    total_hits = cur_hits + prev_hits
    total_fetched = cur_fetched + prev_fetched

    return _format_markdown(
        top=top,
        period=period,
        year=year,
        reprt_code=reprt_code,
        universe=universe,
        fs_div=fs_div,
        sort_by=sort_by,
        direction=direction,
        top_n=top_n,
        universe_size=universe_size,
        data_count=data_count,
        missing=missing,
        api_calls=total_api,
        cache_hits=total_hits,
        api_fetched=total_fetched,
        warnings=[w for w in (universe_warn, big_universe_warn) if w],
    )


def _format_markdown(
    *,
    top: list[ScanRow],
    period: str,
    year: int,
    reprt_code: str,
    universe: str,
    fs_div: str,
    sort_by: str,
    direction: str,
    top_n: int,
    universe_size: int,
    data_count: int,
    missing: int,
    api_calls: int,
    cache_hits: int,
    api_fetched: int,
    warnings: list[str],
) -> str:
    fs_label = {"CFS": "연결", "OFS": "별도"}.get(fs_div, fs_div)
    uni_label = universe.upper() if universe.lower() in ("all", "kospi", "kosdaq") else "지정 리스트"
    prev_label = period_label(year - 1, reprt_code)

    lines = [
        f"# 분기 실적 스캐닝 — {period} ({uni_label}, {fs_label} 기준)",
        "",
        f"조회 회사: {universe_size} / 데이터 보유: {data_count} / "
        f"정렬: {sort_by} {direction} / Top {min(top_n, len(top))}",
        "",
        "| 순위 | 회사 (corp_code) | 매출 | 매출 YoY | 영업이익 | OP YoY | "
        "순이익 | NI YoY | OP 마진 | 비고 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, r in enumerate(top, 1):
        lines.append(
            f"| {i} | {r.corp_name} ({r.corp_code}) "
            f"| {fmt_won(r.rev)} | {fmt_pct(r.rev_yoy)} "
            f"| {fmt_won(r.op)} | {fmt_pct(r.op_yoy)} "
            f"| {fmt_won(r.ni)} | {fmt_pct(r.ni_yoy)} "
            f"| {fmt_pct(r.op_margin) if r.op_margin is not None else 'N/A'} "
            f"| {r.note} |"
        )
    if not top:
        lines.append("| - | (데이터 없음) | - | - | - | - | - | - | - | - |")

    lines.append("")
    lines.append(
        f"_금액 단위: 조/억 자동 절사. YoY는 전년동기({prev_label}) 대비. "
        "Q2 이후는 누적 기준(분기 환산은 v2)._"
    )
    lines.append(
        f"_데이터 결측 {missing}건 · 캐시 hit {cache_hits} / API fetch {api_fetched} "
        f"· API 호출 {api_calls}회._"
    )
    for w in warnings:
        lines.append(f"_⚠️ {w}_")
    return "\n".join(lines)
