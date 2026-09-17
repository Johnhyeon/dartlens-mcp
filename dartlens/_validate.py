"""공통 입력 검증 — 호출 측에서 ValueError 던지면 safe_tool이 사용자 친화적 메시지로 변환."""

from __future__ import annotations

from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def kst_today(now: datetime | None = None) -> date:
    """한국 날짜. DART 접수일·공시 기간은 KST 기준이다.

    PC 날짜(date.today())를 쓰면 미국에 있는 구매자는 한국보다 하루 늦어, "최근 N일"
    조회가 한국 오늘 접수분을 빼고 끝난다. now 는 테스트에서 해외 시각을 흉내 낼 때만.
    """
    return (now or datetime.now(timezone.utc)).astimezone(_KST).date()


def normalize_corp_code(value: str | None) -> str:
    """DART corp_code: 8자리 숫자."""
    if value is None:
        raise ValueError("corp_code가 비어있습니다. 먼저 search_company로 회사를 확정하세요.")
    s = str(value).strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(
            f"corp_code는 정확히 8자리 숫자여야 합니다 (받음: '{s}', 길이 {len(s)}). "
            "6자리 종목코드(stock_code)를 받았다면 search_company로 corp_code를 먼저 확정하세요."
        )
    return s


def normalize_stock_code(value: str | None) -> str:
    """한국거래소 종목코드: 6자리 영숫자."""
    if value is None:
        raise ValueError("stock_code가 비어있습니다.")
    s = str(value).strip()
    if len(s) != 6 or not s.isalnum():
        raise ValueError(f"stock_code는 6자리 영숫자여야 합니다 (받음: '{s}').")
    return s


def normalize_yyyymmdd(value: str | None, *, field: str = "date") -> str:
    """DART 날짜 파라미터는 YYYYMMDD."""
    if value is None:
        raise ValueError(f"{field}가 비어있습니다.")
    s = str(value).strip().replace("-", "").replace("/", "").replace(".", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"{field}는 YYYYMMDD 형식이어야 합니다 (받음: '{value}').")
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError as e:
        raise ValueError(f"{field}가 유효한 날짜가 아닙니다 (받음: '{value}'): {e}")
    return s


def normalize_bsns_year(value) -> str:
    """DART 사업연도: 4자리 숫자 (1980 이후, 미래 1년까지 허용).

    DART 정기보고서는 2015년 이후 데이터가 신뢰할 만하지만, 검증은 1980 이후로 느슨하게.
    """
    s = str(value).strip()
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f"bsns_year는 4자리 연도여야 합니다 (받음: '{value}').")
    year = int(s)
    current = kst_today().year
    if year < 1980 or year > current + 1:
        raise ValueError(f"bsns_year가 비정상 범위입니다: {year} (1980 ~ {current + 1}).")
    return s


# DART reprt_code: 정기보고서 식별
# https://opendart.fss.or.kr 가이드 참조
_REPRT_LABEL_TO_CODE: dict[str, str] = {
    # 영문
    "annual": "11011", "year": "11011",
    "q1": "11013", "1q": "11013",
    "h1": "11012", "semiannual": "11012", "half": "11012",
    "q3": "11014", "3q": "11014",
    # 한글
    "사업": "11011", "사업보고서": "11011", "연간": "11011",
    "1분기": "11013", "분기": "11013",  # 분기만 단독이면 1분기로 해석
    "반기": "11012",
    "3분기": "11014",
}
_VALID_REPRT_CODES = {"11011", "11012", "11013", "11014"}


def normalize_reprt_code(value: str | None) -> str:
    """정기보고서 코드 정규화. 친근 라벨 / 한글 / raw 코드 모두 허용."""
    if value is None:
        raise ValueError("reprt_code가 비어있습니다 (annual / Q1 / H1 / Q3 또는 한글 라벨).")
    s = str(value).strip()
    if s in _VALID_REPRT_CODES:
        return s
    # 영문은 lower 비교
    if s.lower() in _REPRT_LABEL_TO_CODE:
        return _REPRT_LABEL_TO_CODE[s.lower()]
    if s in _REPRT_LABEL_TO_CODE:
        return _REPRT_LABEL_TO_CODE[s]
    raise ValueError(
        f"알 수 없는 reprt_code '{value}'. "
        "사용 가능: annual(사업), Q1(1분기), H1(반기), Q3(3분기), 또는 raw 11011/11012/11013/11014."
    )


def reprt_code_label(code: str) -> str:
    """raw 코드 → 사람이 읽을 수 있는 한글 라벨 (출력용)."""
    return {
        "11011": "사업보고서",
        "11012": "반기보고서",
        "11013": "1분기보고서",
        "11014": "3분기보고서",
    }.get(code, code)


def normalize_rcept_no(value: str | None) -> str:
    """공시 접수번호: 14자리 숫자."""
    if value is None:
        raise ValueError("rcept_no가 비어있습니다. list_disclosures 결과에서 얻으세요.")
    s = str(value).strip()
    if len(s) != 14 or not s.isdigit():
        raise ValueError(f"rcept_no는 14자리 숫자여야 합니다 (받음: '{s}').")
    return s


def normalize_fs_div(value: str | None) -> str:
    """재무제표 구분: CFS(연결) 또는 OFS(별도)."""
    if value is None:
        return "CFS"
    s = str(value).strip().upper()
    aliases = {
        "CFS": "CFS", "연결": "CFS", "CONSOLIDATED": "CFS",
        "OFS": "OFS", "별도": "OFS", "SEPARATE": "OFS", "STANDALONE": "OFS",
    }
    if s in aliases:
        return aliases[s]
    raise ValueError(f"fs_div는 CFS(연결) 또는 OFS(별도)여야 합니다 (받음: '{value}').")


# DART sj_div: 재무제표 종류
_SJ_DIV_ALIASES: dict[str, str] = {
    "BS": "BS", "재무상태표": "BS", "balance": "BS", "balance_sheet": "BS",
    "IS": "IS", "손익계산서": "IS", "income": "IS",
    "CIS": "CIS", "포괄손익계산서": "CIS", "comprehensive_income": "CIS",
    "CF": "CF", "현금흐름표": "CF", "cash_flow": "CF", "cashflow": "CF",
    "SCE": "SCE", "자본변동표": "SCE", "equity": "SCE",
}


def normalize_sj_div(value: str | None) -> str | None:
    """재무제표 종류 필터. None이면 전체."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    s = str(value).strip()
    if s in _SJ_DIV_ALIASES:
        return _SJ_DIV_ALIASES[s]
    if s.lower() in _SJ_DIV_ALIASES:
        return _SJ_DIV_ALIASES[s.lower()]
    raise ValueError(
        f"알 수 없는 sj_div '{value}'. "
        "사용 가능: BS(재무상태표), IS(손익계산서), CIS(포괄손익), CF(현금흐름표), SCE(자본변동표)."
    )


def days_to_range(days: int, *, today: date | None = None) -> tuple[str, str]:
    """한국 오늘 기준 N일 전 ~ 오늘을 (bgn_de, end_de) YYYYMMDD로 반환.

    today 를 안 주면 KST 날짜를 쓴다. 예전엔 PC 날짜라 미국 시간대 사용자의 조회가
    한국 어제에서 끝났다.
    """
    if not isinstance(days, int) or days < 1 or days > 3650:
        raise ValueError(f"days는 1~3650 사이의 정수여야 합니다 (받음: {days}).")
    from datetime import timedelta
    today = today or kst_today()
    bgn = today - timedelta(days=days)
    return bgn.strftime("%Y%m%d"), today.strftime("%Y%m%d")
