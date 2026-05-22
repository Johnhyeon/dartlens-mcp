"""scan_earnings_season 핵심 로직 — period 파싱 / universe 해석 /
다중회사 병렬 fetch / YoY 계산 / 정렬 / 마크다운 포매팅.

server.py는 이 모듈의 run_scan을 얇게 감싸기만 한다 (_order_backlog.py와 동일 패턴).
순수 함수(parse_period / extract_accounts / compute_row / sort)는 테스트에서
네트워크 없이 직접 검증한다.
"""

from __future__ import annotations

import asyncio
import re
import statistics
from dataclasses import dataclass

from dartlens._cache import EarningsCache, get_earnings_cache
from dartlens._corp_code import all_listed, corp_basic_map
from dartlens._http import get_multi_acnt
from dartlens._market import market_stock_codes, meta_map
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

    "all" → 상장사 전체. "kospi"/"kosdaq" → KRX 시장구분 매핑으로 필터
    (매핑 fetch 실패 + 캐시 없음 시에만 전체 폴백 + 경고). 콤마 리스트
    또는 단일 corp_code → 그대로.
    """
    u = (universe or "").strip()
    if not u:
        raise ValueError("universe가 비어있습니다 (all/kospi/kosdaq 또는 corp_code 리스트).")

    low = u.lower()
    if low in ("all", "kospi", "kosdaq"):
        entries = [e for e in await all_listed() if e.is_listed]
        if not entries:
            raise ValueError("corpCode.xml에서 상장사를 찾지 못했습니다 (캐시 손상?).")

        if low == "all":
            return sorted({e.corp_code for e in entries}), None

        # kospi/kosdaq — KRX 시장구분으로 stock_code 필터
        codes_set = await market_stock_codes(low)
        if not codes_set:
            # KRX fetch 실패 + 캐시 없음 → 전체 상장사 폴백
            return (
                sorted({e.corp_code for e in entries}),
                f"KRX 시장구분을 가져오지 못해 '{low}' 요청을 전체 상장사로 "
                f"폴백했습니다 (네트워크/캐시 확인).",
            )
        filtered = sorted(
            {e.corp_code for e in entries if e.stock_code in codes_set}
        )
        if not filtered:
            return (
                sorted({e.corp_code for e in entries}),
                f"'{low}' 교집합이 비어 전체 상장사로 폴백 (KRX↔corpCode 종목코드 불일치?).",
            )
        return filtered, None

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


def _short(s: str, n: int) -> str:
    """마크다운 셀용 절단. 파이프(|)는 표 깨지므로 / 로 치환."""
    s = (s or "").replace("|", "/").replace("\n", " ").strip()
    if not s:
        return "-"
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_yyyymmdd(s: str) -> str:
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _receipt_metadata(row: dict) -> tuple[str, str]:
    """DART 접수번호/접수일 메타.

    fnlttMultiAcnt는 접수일 컬럼이 없을 수 있어 rcept_no 앞 8자리(YYYYMMDD)를
    접수일로 보완한다. 접수번호 자체는 원문 대조와 후속 공시 조회용으로 보존.
    """
    rcept_no = (row.get("rcept_no") or "").strip()
    raw_dt = (row.get("rcept_dt") or "").strip()
    if not raw_dt and len(rcept_no) >= 8 and rcept_no[:8].isdigit():
        raw_dt = rcept_no[:8]
    return rcept_no, _fmt_yyyymmdd(raw_dt)


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
    반환: {corp_name, rcept_no, filing_date, rev_cur, rev_prev, op_cur,
    op_prev, ni_cur, ni_prev}
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
    rcept_no, filing_date = _receipt_metadata(corp_rows[0])
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

    out: dict = {"corp_name": corp_name, "rcept_no": rcept_no, "filing_date": filing_date}
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
    rcept_no: str = ""
    filing_date: str = ""
    flagged: bool = False
    sector: str = ""   # KRX 업종(KSIC)
    product: str = ""  # KRX 주요제품(free-text)


# 비현실적 금액 임계 = 1,000조(원). 한국 최대 기업(삼성전자) 연 매출도
# ~300조, 분기 ~130조 수준이라 정상 필링은 절대 도달 불가. 관측된 오류
# 필링은 ~87,000조(천원/원 단위 오기재 추정)라 1e15에서 깨끗이 분리.
# 정책: 값을 조작·클램프하지 않고 비고에 ⚠원본확인 플래그만 — 출처 충실 반영.
_IMPLAUSIBLE_WON = 1_000_000_000_000_000


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

    # 비현실적 규모 → DART 원본 기재 오류 의심. 값은 그대로 두고 플래그만.
    flagged = any(
        v is not None and abs(v) >= _IMPLAUSIBLE_WON for v in (rev, op, ni)
    )
    if flagged:
        notes.insert(0, "⚠원본확인")

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
        rcept_no=acc.get("rcept_no") or "",
        filing_date=acc.get("filing_date") or "",
        flagged=flagged,
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
    group_by: str | None = None,
    cache: EarningsCache | None = None,
) -> str:
    """scan_earnings_season 본체. 마크다운 str 반환.

    group_by="sector"면 KSIC 업종별 집계(중앙값) 테이블, 그 외엔
    종목별 테이블(주요제품 컬럼 포함).
    """
    year, reprt_code = parse_period(period)
    if group_by not in (None, "", "sector"):
        raise ValueError(f"group_by는 None 또는 'sector'여야 합니다 (받음: '{group_by}').")

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

    # fnlttMultiAcnt엔 corp_name이 없다 → corpCode.xml에서 name+stock_code,
    # stock_code로 KRX 메타(시장/업종/주요제품) 일괄 조인
    basic = await corp_basic_map([r.corp_code for r in rows])
    stock_codes = [sc for _, sc in basic.values() if sc]
    metas = await meta_map(stock_codes)
    for r in rows:
        name, sc = basic.get(r.corp_code, ("", ""))
        if not r.corp_name:
            r.corp_name = name or r.corp_code
        m = metas.get(sc) if sc else None
        if m:
            r.sector = m.get("sector", "")
            r.product = m.get("product", "")

    data_count = len(rows)
    flagged_count = sum(1 for r in rows if r.flagged)

    missing = universe_size - data_count
    total_api = cur_calls + prev_calls
    total_hits = cur_hits + prev_hits
    total_fetched = cur_fetched + prev_fetched
    warnings = [w for w in (universe_warn, big_universe_warn) if w]

    if group_by == "sector":
        return _format_sector_markdown(
            rows=rows,
            period=period,
            universe=universe,
            fs_div=fs_div,
            direction=direction,
            top_n=top_n,
            universe_size=universe_size,
            data_count=data_count,
            missing=missing,
            flagged_count=flagged_count,
            api_calls=total_api,
            cache_hits=total_hits,
            api_fetched=total_fetched,
            warnings=warnings,
        )

    sorted_rows = sort_rows(rows, sort_by, direction)
    top = sorted_rows[:top_n]
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
        flagged_count=flagged_count,
        api_calls=total_api,
        cache_hits=total_hits,
        api_fetched=total_fetched,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 섹터 집계 (group_by="sector")
# ---------------------------------------------------------------------------

# 통계적으로 "섹터가 잘 나왔다"가 의미 있으려면 표본이 필요.
_MIN_SECTOR_FIRMS = 3


@dataclass
class SectorAgg:
    sector: str
    n: int                       # 데이터 보유 회사 수
    op_inc_ratio: float | None   # 영익 YoY > 0 회사 / YoY 산출 가능 회사
    turnaround: int              # 흑전(영업/순익) 회사 수
    turn_ratio: float            # turnaround / n
    op_yoy_median: float | None  # 영익 YoY 중앙값 (이상치 면역)
    rev_yoy_median: float | None


def aggregate_sectors(rows: list[ScanRow]) -> list[SectorAgg]:
    """KSIC 업종별 집계. 평균이 아닌 **중앙값** — 87,243조 오기재나
    전년바닥 +16만% 같은 이상치가 섹터 통계를 망치지 않게.
    회사 _MIN_SECTOR_FIRMS개 미만 업종은 통계 무의미라 제외(footer에 안내).
    """
    by_sector: dict[str, list[ScanRow]] = {}
    for r in rows:
        sec = r.sector.strip() if r.sector else ""
        if not sec:
            continue
        by_sector.setdefault(sec, []).append(r)

    out: list[SectorAgg] = []
    for sec, group in by_sector.items():
        if len(group) < _MIN_SECTOR_FIRMS:
            continue
        turn = sum(1 for r in group if "흑전" in r.note)
        op_yoys = [r.op_yoy for r in group if r.op_yoy is not None]
        rev_yoys = [r.rev_yoy for r in group if r.rev_yoy is not None]
        # 영익 증가 비율: 흑전(적자→흑자)과 별개 — 흑자였든 적자였든
        # 작년보다 영익이 늘었나. 흑전비율로는 못 잡는 "이미 흑자인데
        # 더 좋아진 업종"을 드러낸다. YoY 산출 불가(전년 0/결측) 회사는
        # 분모에서 제외 — 중앙값과 동일 모집단.
        op_inc_ratio = (
            sum(1 for v in op_yoys if v > 0) / len(op_yoys)
            if op_yoys
            else None
        )
        out.append(
            SectorAgg(
                sector=sec,
                n=len(group),
                op_inc_ratio=op_inc_ratio,
                turnaround=turn,
                turn_ratio=turn / len(group),
                op_yoy_median=statistics.median(op_yoys) if op_yoys else None,
                rev_yoy_median=statistics.median(rev_yoys) if rev_yoys else None,
            )
        )
    return out


def _format_sector_markdown(
    *,
    rows: list[ScanRow],
    period: str,
    universe: str,
    fs_div: str,
    direction: str,
    top_n: int,
    universe_size: int,
    data_count: int,
    missing: int,
    flagged_count: int,
    api_calls: int,
    cache_hits: int,
    api_fetched: int,
    warnings: list[str],
) -> str:
    fs_label = {"CFS": "연결", "OFS": "별도"}.get(fs_div, fs_div)
    uni_label = (
        universe.upper()
        if universe.lower() in ("all", "kospi", "kosdaq")
        else "지정 리스트"
    )
    aggs = aggregate_sectors(rows)
    # 영익증가비율 desc → 동률이면 영익YoY중앙값 → 흑전비율 순.
    # "전반적으로 좋았던 업종"의 1차 기준은 흑전(턴어라운드)이 아니라
    # 영익이 늘어난 회사 비중. 산출불가(None)는 -inf로 맨 뒤.
    reverse = direction != "asc"
    aggs.sort(
        key=lambda a: (
            a.op_inc_ratio if a.op_inc_ratio is not None else float("-inf"),
            a.op_yoy_median if a.op_yoy_median is not None else float("-inf"),
            a.turn_ratio,
        ),
        reverse=reverse,
    )
    top = aggs[:top_n]
    sectors_with_data = {r.sector for r in rows if r.sector}
    excluded = len(sectors_with_data) - len(aggs)

    lines = [
        f"# 섹터 실적 스캐닝 — {period} ({uni_label}, {fs_label} 기준)",
        "",
        f"조회 회사: {universe_size} / 데이터 보유: {data_count} / "
        f"집계 섹터: {len(aggs)}개(회사 {_MIN_SECTOR_FIRMS}+ 기준) / "
        f"정렬: 영익증가비율 {direction} / Top {min(top_n, len(top))}",
        "",
        "| 순위 | 섹터 (KSIC 업종) | 회사수 | 영익↑비율 | 흑전 | 흑전비율 | "
        "영익 YoY 중앙 | 매출 YoY 중앙 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for i, a in enumerate(top, 1):
        inc = f"{a.op_inc_ratio * 100:.0f}%" if a.op_inc_ratio is not None else "N/A"
        lines.append(
            f"| {i} | {_short(a.sector, 28)} | {a.n} | {inc} | {a.turnaround} "
            f"| {a.turn_ratio * 100:.0f}% "
            f"| {fmt_pct(a.op_yoy_median)} | {fmt_pct(a.rev_yoy_median)} |"
        )
    if not top:
        lines.append("| - | (집계 가능 섹터 없음) | - | - | - | - | - | - |")

    lines.append("")
    lines.append(
        "_영익↑비율 = (영익 YoY > 0 회사) / (YoY 산출 가능 회사) — 흑자였든 "
        "적자였든 작년보다 영익이 늘었나. 흑전비율 = (영업/순익 흑전 회사) "
        "/ (섹터 내 데이터 보유 회사) — 적자→흑자만. 둘은 다른 시그널. "
        "YoY는 **중앙값**(이상치 면역). 테마(원전·AI반도체 등)는 KSIC를 "
        "가로질러 안 잡힘 — 개별 종목·사업보고서로 확인._"
    )
    lines.append(
        f"_시간축: 실적 기간은 {period}, 공시 접수일은 회사별로 다릅니다. "
        "섹터 집계는 공시일을 섞어 본 요약이므로 주가·수급 반응은 종목별 "
        "scan 결과의 공시 접수일 기준 전후 거래일로 비교하세요._"
    )
    lines.append(
        f"_데이터 결측 {missing}건 · 회사 {_MIN_SECTOR_FIRMS}개 미만 "
        f"제외 섹터 {excluded}개 · 캐시 hit {cache_hits} / API fetch "
        f"{api_fetched} · API 호출 {api_calls}회._"
    )
    if flagged_count:
        lines.append(
            f"_⚠원본확인 {flagged_count}건: 비현실적 규모 원본 오류 의심 "
            "(중앙값 집계라 섹터 통계엔 영향 미미)._"
        )
    for w in warnings:
        lines.append(f"_⚠️ {w}_")
    return "\n".join(lines)


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
    flagged_count: int,
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
        "| 순위 | 회사 (corp_code) | 공시일 | 주요제품 | 매출 | 매출 YoY | 영업이익 | OP YoY | "
        "순이익 | NI YoY | OP 마진 | 비고 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, r in enumerate(top, 1):
        filing = r.filing_date or "N/A"
        if r.rcept_no:
            filing = f"{filing} (`rcept_no={r.rcept_no}`)"
        lines.append(
            f"| {i} | {r.corp_name} ({r.corp_code}) "
            f"| {filing} "
            f"| {_short(r.product, 28)} "
            f"| {fmt_won(r.rev)} | {fmt_pct(r.rev_yoy)} "
            f"| {fmt_won(r.op)} | {fmt_pct(r.op_yoy)} "
            f"| {fmt_won(r.ni)} | {fmt_pct(r.ni_yoy)} "
            f"| {fmt_pct(r.op_margin) if r.op_margin is not None else 'N/A'} "
            f"| {r.note} |"
        )
    if not top:
        lines.append("| - | (데이터 없음) | - | - | - | - | - | - | - | - | - | - |")

    lines.append("")
    lines.append(
        f"_금액 단위: 조/억 자동 절사. YoY는 전년동기({prev_label}) 대비. "
        "Q2 이후는 누적 기준(분기 환산은 v2)._"
    )
    lines.append(
        f"_시간축: 실적 기간은 {period}, 공시 접수일은 회사별로 다릅니다. "
        "주가·수급 반응은 마감일 일괄 기준이 아니라 각 회사의 공시 접수일 "
        "기준 전후 거래일로 비교하세요. StockLens가 함께 설치되어 있으면 "
        "event_date=공시일로 이어서 확인하세요._"
    )
    lines.append(
        f"_데이터 결측 {missing}건 · 캐시 hit {cache_hits} / API fetch {api_fetched} "
        f"· API 호출 {api_calls}회._"
    )
    if flagged_count:
        lines.append(
            f"_⚠원본확인 {flagged_count}건: 매출/이익이 비현실적 규모 "
            "(1,000조↑) — DART 원본 기재 오류 의심. 값은 미가공, corp_code로 원문 대조 권장._"
        )
    for w in warnings:
        lines.append(f"_⚠️ {w}_")
    return "\n".join(lines)
