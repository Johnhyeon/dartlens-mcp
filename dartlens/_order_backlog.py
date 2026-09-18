"""Order backlog extraction from DART document tables."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from dartlens._document_tables import DocumentTable


BACKLOG_KEYWORDS = ("수주잔고", "수주잔액", "계약잔액", "계약잔고", "남은 수행의무")
# 공사·계약 상세표임을 알려주는 머리글 낱말(회사마다 다르게 적는다)
_DETAIL_HEADER_WORDS = ("품목", "발주처", "구분", "현장명", "공사명", "사업명", "계약명")
ENDING_BALANCE_KEYWORDS = ("기말계약잔액", "기말공사계약잔액", "기말공사 계약잔액", "기말잔액")


@dataclass(frozen=True)
class OrderBacklogPoint:
    period: str
    value: float


@dataclass(frozen=True)
class BacklogSnapshot:
    """한 보고서에서 뽑은 수주잔고 값 + 원문 근거.

    실측(두산에너빌리티 2025 사업보고서): 예전 파서는 계약별 상세표의 마지막
    열(진행률 %)을 금액으로 합산해 261.6억원을 만들었다. 같은 표의 단일 계약
    (체코, 4.8조원)보다 작은 값이 전체 잔고로 나갔다. 값만 돌려주면 이런
    오류를 아무도 못 잡으므로, 어떤 표에서 몇 행을 어떤 단위로 읽었는지를
    함께 들고 다닌다.
    """

    point: OrderBacklogPoint
    tables: list[dict]          # {caption, unit, source_rows, rows_used, raw_sum, eok_sum, method}
    warnings: list[str]
    max_single_detail: float    # value_unit 기준. 전체 잔고가 이보다 작으면 말이 안 된다
    anomalous: bool = False
    unit_unknown: bool = False  # 합산한 표 중에 단위 표기가 없는 게 있다
    value_unit: str = "억원"    # 외화 표는 원문 단위 그대로(환산하지 않는다)
    unit_source: str = "declared"  # "declared" = 원문에 단위 표기 있음 / "assumed" = 억원 가정
    basis: str = ""             # "연결"/"별도" — 섞으면 없던 증감이 생긴다
    method: str = ""            # ending_balance / contract_detail / single_value


@dataclass(frozen=True)
class OrderBacklogSeries:
    metric: str
    unit: str
    points: list[OrderBacklogPoint]
    table_caption: str = ""
    unit_source: str = "declared"  # "declared" = 표에 단위 표기 있음 / "assumed" = 없음
    source_unit: str | None = None  # 원문 표의 단위 표기(없으면 None)
    basis: str = ""                 # "연결"/"별도" — 모르면 빈 문자열


def extract_order_backlog_series(tables: list[DocumentTable], *, limit: int = 3) -> OrderBacklogSeries | None:
    for table in tables:
        series = _extract_from_table(table, limit=limit)
        if series is not None:
            return series
    return None


def extract_order_backlog_point(tables: list[DocumentTable], *, period: str) -> OrderBacklogPoint | None:
    snap = extract_order_backlog_snapshot(tables, period=period)
    return snap.point if snap is not None else None


def extract_order_backlog_snapshot(
    tables: list[DocumentTable], *, period: str
) -> BacklogSnapshot | None:
    """보고서의 표들에서 수주잔고 한 점을 근거와 함께 뽑는다.

    우선순위: 계약 변동내역의 기말잔액 > 계약별 상세표(부문별 합산) >
    단일 값 표. 상세표는 여러 부문이 표로 나뉘므로 전부 합치되 동일한 표는
    한 번만 센다.
    """
    # 1) 기말계약잔액 - 원문이 스스로 합계를 말해주는 가장 신뢰되는 형태
    chosen = _choose_ending_table(tables)
    if chosen is not None:
        table, found = chosen
        return _single_row_snapshot(
            table, found, period=period, method="ending_balance")

    # 2) 계약별 상세표 - 부문별 표를 전부 합친다 (동일 표 dedup)
    seen: set[int] = set()
    failed_keys: set[int] = set()
    extracted: list[dict] = []
    warnings: list[str] = []
    for table in tables:
        if not _table_has_backlog_context(table):
            continue
        if _is_intangible_backlog_table(table):
            continue
        key = hash(tuple(tuple(row) for row in table.rows))
        if key in seen:
            continue
        info = _contract_detail_extract(table)
        if info is None:
            continue
        seen.add(key)
        if info.get("_failed"):
            failed_keys.add(key)
            warnings.append(
                f"표 '{info['caption'][:40]}'는 수주총액=기납품액+수주잔고 검산이 "
                "성립하지 않아 값을 추출하지 않았습니다(열 구성 확인 필요)."
            )
            continue
        extracted.append(info)
        warnings.extend(info.pop("_warnings"))
    if extracted:
        # 같은 수주잔고를 부문별 요약표와 계약별 상세표로 두 번 싣는 보고서가
        # 있다. 실측(한화에어로스페이스 2025 사업보고서 20260316001112): 요약표
        # 합계 116,800,729천원과 상세표 '계' 116,800,728천원이 같은 값인데, 그대로
        # 더해 168.8조가 나갔다(원문 116.8조). 명시 합계가 같은 표는 한 번만 센다.
        # 한 보고서가 같은 공사를 두 표에 싣는 경우가 있다.
        # - 금호건설 20240318000777: 전체 공사 145행 표(총계 7.1조) 옆에 주요
        #   공사 26건만 뽑은 표가 따로 있다.
        # - 두산에너빌리티 20260320001246: 같은 공사 목록이 당기·전기 두 벌이다
        #   (공사명 36개 중 34개가 같다). 예전엔 둘을 더해 14.8조가 26.1조로 나갔다.
        # 행 이름이 더 많은 표를 남기고, 그 표에 대부분 들어 있는 표는 뺀다.
        # 금액이 아니라 포함 관계로 고른다 - 잔고가 줄어든 해에는 전기 쪽이 더
        # 클 수 있어서 큰 값을 남기면 옛날 표가 남는다.
        by_size = sorted(extracted,
                         key=lambda i: (len(i.get("_keys") or ()), i["eok_sum"]),
                         reverse=True)
        keep_ids: list[int] = []
        for info in by_size:
            keys = info.get("_keys") or set()
            # 행 이름이 두어 개뿐인 표(예: 관계사/비관계사 구분표)는 이름이 같아도
            # 같은 내역이라는 근거가 못 된다. 목록다운 표에만 적용한다.
            covered = len(keys) >= 3 and any(
                len(keys & (other.get("_keys") or set())) >= len(keys) * 0.7
                for other in by_size if id(other) in keep_ids
            )
            if covered:
                warnings.append(
                    f"표 '{(info.get('caption') or '무제')[:30]}'의 공사들이 더 큰 표에 "
                    "그대로 들어 있어 같은 내역을 발췌한 목록으로 보고 합산에서 뺐습니다."
                )
                continue
            keep_ids.append(id(info))
        extracted = [i for i in extracted if id(i) in keep_ids]

        deduped: list[dict] = []
        for info in extracted:
            twin = next(
                (d for d in deduped
                 if d.get("unit") == info.get("unit")
                 and _same_amount_rel(d["eok_sum"], info["eok_sum"])),
                None,
            )
            if twin is None:
                deduped.append(info)
            else:
                warnings.append(
                    f"표 '{(info.get('caption') or '무제')[:30]}'의 합계가 "
                    f"'{(twin.get('caption') or '무제')[:30]}'와 같아 같은 내역을 "
                    "요약·상세로 두 번 실은 것으로 보고 한 번만 셌습니다."
                )
        extracted = deduped

        # 통화가 섞이면 합칠 수 없다. 원화 표가 있으면 원화만 쓰고 외화 표는
        # 제외를 알린다. 원화가 없으면 외화 단위 그대로(환산하지 않고) 낸다.
        krw = [i for i in extracted if i.get("currency") == "KRW"]
        if krw and len(krw) < len(extracted):
            dropped = [i for i in extracted if i.get("currency") != "KRW"]
            warnings.append(
                "외화 표 " + str(len(dropped)) + "개("
                + ", ".join(sorted({i["unit"] for i in dropped}))
                + ")는 원화 합계에서 제외했습니다. 통화가 달라 합칠 수 없습니다."
            )
            extracted = krw
        # 부문별 표 여러 개를 더해 회사 합계를 만드는 자리다. 여기서는 표마다
        # 단위가 밝혀져 있어야 한다 - 한 표만 백만원을 억원으로 읽어도 그 표가
        # 합계를 통째로 지배한다. 실측(현대건설 [기재정정]사업보고서
        # 20251017000151): '(2) 현대엔지니어링' 표만 단위 표기가 없어
        # 191,004억이 19,100,399억이 됐고 그 해 수주잔고가 1,938조로 나갔다.
        # 반기보고서(20250814002545)는 두 표 다 표기가 없어 4,337조가 됐다.
        # 원문에 그 표들의 단위는 실제로 없다. 가까운 표에서 물려받아 추측하지
        # 않고, 합계를 만들지 않는다(한 표만 읽는 다른 경로는 종전대로
        # '억원 가정'으로 표시하고 값을 낸다).
        unknown = [i for i in extracted if i.get("unit_source") == "assumed"]
        unit_unknown = bool(unknown)
        if unit_unknown:
            warnings.append(
                "표 " + str(len(unknown)) + "개("
                + ", ".join(sorted({(i.get("caption") or "무제")[:20] for i in unknown}))
                + ")에 단위 표기가 없어 부문 합계를 만들지 않았습니다. "
                "단위를 잘못 읽으면 100배·10만배 어긋납니다."
            )
        units = sorted({i["unit"] for i in extracted if i.get("currency") != "KRW"})
        value_unit = units[0] if units else "억원"
        value = round(sum(i["eok_sum"] for i in extracted), 2)
        max_detail = max(i["_max_detail"] for i in extracted)
        for i in extracted:
            i.pop("_max_detail", None)
            i.pop("_keys", None)
        anomalous = value < max_detail * 0.999
        if anomalous:
            warnings.append(
                f"추출된 전체 잔고({_format_value(value)}억원)가 단일 세부 계약 "
                f"최대값({_format_value(round(max_detail, 2))}억원)보다 작습니다. "
                "표 범위·단위·행 선택 오류 가능성이 있어 전체 잔고로 확정하지 않습니다."
            )
        bases = {i.get("basis") or "" for i in extracted}
        return BacklogSnapshot(
            point=OrderBacklogPoint(period=period, value=value),
            tables=extracted,
            warnings=warnings,
            max_single_detail=round(max_detail, 2),
            anomalous=anomalous,
            unit_unknown=unit_unknown,
            value_unit=value_unit,
            unit_source=(
                "assumed" if any(i.get("unit_source") == "assumed" for i in extracted)
                else "declared"
            ),
            basis=bases.pop() if len(bases) == 1 else "",
            method="contract_detail",
        )

    # 3) 단일 값 표 - 검산에 실패한 상세표는 여기서도 쓰지 않는다
    for table in tables:
        if hash(tuple(tuple(row) for row in table.rows)) in failed_keys:
            continue
        found = _fallback_value_from_table(table)
        if found is not None:
            return _single_row_snapshot(
                table, found, period=period, method="single_value")
    return None


def _choose_ending_table(
    tables: list[DocumentTable],
) -> tuple[DocumentTable, tuple[float, list[str]]] | None:
    """기말계약잔액 표가 여러 벌일 때 어느 것을 읽을지 정한다.

    한 보고서에 같은 표가 연결·별도 x 당기·전기로 최대 네 벌 실린다. 원문
    순서는 믿을 수 없다 - 첨부(감사보고서·연결감사보고서)가 본문보다 앞에
    오는 해가 있어서, 그냥 첫 표를 쓰면 2024년은 연결·2025년은 별도가 한
    시계열에 섞인다(실측: 현대무벡스 2024·2025 사업보고서).

    - 기준: 연결 우선(DART 재무 도구 기본값 CFS 와 같다). 없으면 별도.
    - 기간: 당기. 전기 표의 기말은 당기 표의 기초와 같으므로, 내 기말이 다른
      표의 기초로 쓰였다면 내가 전기다. 판정이 안 되면 원문 순서를 따른다.
    """
    candidates = []
    for table in tables:
        found = _ending_value_from_table(table)
        if found is None:
            continue
        candidates.append((table, found, _opening_value_from_table(table)))
    if not candidates:
        return None

    same = candidates
    for basis in ("연결", "별도"):
        picked = [c for c in candidates if c[0].basis == basis]
        if picked:
            same = picked
            break

    openings = [c[2] for c in same if c[2] is not None]
    for table, found, _opening in same:
        if not any(_same_amount(found[0], opening) for opening in openings):
            return table, found
    return same[0][0], same[0][1]


def _opening_value_from_table(table: DocumentTable) -> float | None:
    """롤포워드 표의 기초 잔액. 당기/전기 판정에만 쓴다."""
    if not _table_has_backlog_context(table):
        return None
    if _is_intangible_backlog_table(table):
        return None
    default_unit = _table_unit(table)
    for index, row in enumerate(table.rows):
        if _is_opening_balance_row(row) and _metric_name(row) is not None:
            return _balance_value_from_row(
                table.rows, index, default_unit=default_unit)
    return None


def _same_amount(left: float, right: float) -> bool:
    return abs(left - right) <= max(0.01, abs(left) * 1e-9)


def _same_amount_rel(left: float, right: float) -> bool:
    """반올림 차이만 있는 같은 금액인가(요약표와 상세표는 끝자리가 다를 수 있다)."""
    if left <= 0 or right <= 0:
        return False
    return abs(left - right) <= max(0.01, abs(left) * 1e-4)


_ASSUMED_UNIT_WARNING = "표에 단위 표기가 없어 억원으로 가정했습니다. 원문 대조가 필요합니다."


def _foreign_unit_warning(unit: str) -> str:
    return (
        f"외화 표({unit})입니다. 원화로 환산하지 않고 원문 단위 "
        "그대로 보고합니다 - 억원과 나란히 놓고 비교하면 안 됩니다."
    )


def _single_row_snapshot(
    table: DocumentTable, found: tuple[float, list[str]], *, period: str, method: str
) -> BacklogSnapshot:
    """한 행에서 읽은 값에 원문 그대로의 단위를 붙인다.

    예전엔 이 경로가 단위를 늘 '억원'으로 적었다. '(단위: 백만달러)' 표의 12,355 는
    12,355억원으로, 단위 표기가 없는 표의 값은 '원문 표기 기준' 억원으로 나갔다.
    """
    value, row = found
    unit = _table_unit(table)
    warnings: list[str] = []
    if _is_foreign_unit(unit):
        value_unit, unit_source, unit_label = unit, "declared", unit
        warnings.append(_foreign_unit_warning(unit))
    elif unit:
        value_unit, unit_source, unit_label = "억원", "declared", unit
    elif _row_has_inline_unit(row):
        value_unit, unit_source, unit_label = "억원", "declared", "셀 표기"
    else:
        value_unit, unit_source, unit_label = "억원", "assumed", "표기 없음(억원 가정)"
        warnings.append(_ASSUMED_UNIT_WARNING)
    return BacklogSnapshot(
        point=OrderBacklogPoint(period=period, value=value),
        tables=[{
            "caption": table.caption[:80],
            "unit": unit_label,
            "unit_source": unit_source,
            "basis": table.basis,
            # 캡션이 '(단위: 천원)' 한 줄뿐인 표가 많다. 어느 행을 읽었는지가
            # 사람이 원문과 맞춰볼 수 있는 진짜 근거다.
            "row_label": (row[0] if row else "")[:40],
            "source_rows": len(table.rows),
            "rows_used": 1,
            "raw_sum": None,
            "eok_sum": value,
            "method": method,
        }],
        warnings=warnings,
        max_single_detail=value,
        value_unit=value_unit,
        unit_source=unit_source,
        basis=table.basis,
        method=method,
    )


_TOTAL_LABELS = {"합계", "총계", "계", "소계"}
_PLAIN_NUMBER_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
# 셀 전체가 괄호로 싸인 숫자만 음수로 읽는다. '(*1)'·'(단위: 천원)'은 해당 없음.
_PAREN_NUMBER_RE = re.compile(r"^\((\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\)$")


def _plain_number(cell: str) -> float | None:
    """콤마 숫자 셀만 숫자로 읽는다. 날짜('2007-03-09')·라벨은 None.

    한국 재무 표는 음수를 괄호로 적는다. 예전엔 '(10,322,942)'를 숫자로 못 읽어
    행마다 숫자가 두 개뿐이 됐고, 그러면 항등식(수주총액=기납품액+수주잔고)을
    세울 수가 없어 검산이 아예 안 돌았다 - 실측(한화오션 2025 사업보고서
    20260317000644)에서 검산 실패 표시도 안 남은 채 단일 값 경로로 새어,
    합계(34.5조) 대신 첫 행 '상선'(26.0조)이 수주잔고로 나갔다.
    """
    text = (cell or "").strip().replace(" ", "")
    if not text:
        return None
    paren = _PAREN_NUMBER_RE.match(text)
    if paren:
        return -float(paren.group(1).replace(",", ""))
    if not _PLAIN_NUMBER_RE.match(text):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _identity_backlog_column(numeric_rows: list[dict]) -> int | None:
    """행 항등식(수주총액 = 기납품액 + 수주잔고)으로 수주잔고 열을 찾는다.

    표마다 열 구성이 다르다 - 두산은 수주잔고 뒤에 진행률·미청구공사가 붙고,
    현대로템형은 수량/금액 쌍으로 열이 늘어난다. 위치를 가정하는 대신 표 안의
    수학으로 자가검증한다: 열 (a < b < c)에서 v[a] = v[b] + v[c] 가 대다수
    행에서 성립하면 c 가 수주잔고다(열 순서는 원문 표기 순서를 따른다).

    기납품액을 차감액으로 보아 음수로 적는 표가 있다(한화오션). 부호만 다를 뿐
    같은 항등식이므로 가운데 항은 절댓값으로 본다. 수주총액·수주잔고는 음수일
    수 없으니 그 조건은 그대로 둔다.
    """
    votes: dict[int, int] = {}
    checked = 0
    for nums in numeric_rows:
        idxs = sorted(nums)
        if len(idxs) < 3:
            continue
        checked += 1
        for ai in range(len(idxs)):
            for bi in range(ai + 1, len(idxs)):
                for ci in range(bi + 1, len(idxs)):
                    a, b, c = idxs[ai], idxs[bi], idxs[ci]
                    va, vb, vc = nums[a], nums[b], nums[c]
                    if va <= 0 or vc < 0:
                        continue
                    if abs(va - (abs(vb) + vc)) <= max(2.0, va * 0.005):
                        votes[c] = votes.get(c, 0) + 1
    if checked == 0:
        return None, 0
    if not votes:
        return None, checked
    best, count = max(votes.items(), key=lambda kv: kv[1])
    if count >= max(1, int(checked * 0.7)):
        return best, checked
    return None, checked


_TOTAL_REF_RE = re.compile(r"\([^()]*\)$")


def _is_total_row(labels: list[str]) -> bool:
    """라벨 칸 중 하나라도 합계를 뜻하면 합계행이다.

    '합 계'(띄어쓴 것)·'국내합계'·'국내 / 해외 합계'·'계' 가 모두 해당한다.
    라벨 끝에 붙은 참조 기호도 떼고 본다 - 실측(금호건설 2023 사업보고서
    20240318000777)의 합계 행은 '총계(E=C+D)'·'해외합계(D)'·'해외토목 계(A)'
    처럼 적혀 있어, 그대로 보면 전부 세부행으로 세어 7.1조가 33.0조가 됐다.
    """
    for label in labels:
        bare = _TOTAL_REF_RE.sub("", label).strip()
        squeezed = bare.replace(" ", "")
        if squeezed in _TOTAL_LABELS:
            return True
        if squeezed.endswith("합계") or squeezed.endswith("총계"):
            return True
        # '합계 - 전체'처럼 합계를 앞에 적는 표가 있다(태영건설 20240927000935).
        # 그대로 두면 합계행이 세부행으로 섞여 6.2조가 20.9조가 된다.
        if squeezed.startswith(("합계", "총계", "소계")):
            return True
        # '해외토목 계' 처럼 띄어 쓴 소계. '설계'·'통계' 같은 낱말에 걸리지 않게
        # 앞에 띄어쓰기가 있는 '계' 만 본다.
        if bare.endswith(" 계"):
            return True
    return False


_MAX_TOTAL_ROWS_TO_COMBINE = 12


def _grand_total(values: list[float]) -> float | None:
    """합계행이 여럿일 때 '전체 합계' 하나를 고른다.

    수주상황 표는 소계가 여러 단으로 쌓인다 - 금호건설은 국내토목 계, 국내건축
    합계, 국내합계, 해외토목 계, 해외합계, 총계까지 여섯 줄이다. 가장 큰 값이
    나머지 중 어떤 조합의 합과 맞으면 그게 총계다(자가검증: 총계 7,092,545 =
    국내합계 6,992,368 + 해외합계 100,177). 맞는 조합이 없으면 어느 것이
    총계인지 지어내지 않고 없음을 돌려준다.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values, reverse=True)
    biggest, rest = ordered[0], ordered[1:]
    tolerance = max(1.0, abs(biggest) * 0.01)
    if abs(biggest - sum(rest)) <= tolerance:
        return biggest
    if len(rest) <= _MAX_TOTAL_ROWS_TO_COMBINE:
        for size in range(1, len(rest) + 1):
            for combo in itertools.combinations(rest, size):
                if abs(biggest - sum(combo)) <= tolerance:
                    return biggest
    return None


def _is_scenario_twin(left: list[str], right: list[str]) -> bool:
    """같은 계약을 '○○ 기준'으로 두 번 적은 행인가.

    실측(삼성바이오로직스 2025 사업보고서 20260515001658): 같은 CDMO 항체의약품
    계약이 '현 최소구매물량 기준'(수주잔고 10,704)과 '고객사 제품개발 성공시
    예상 수요물량 기준'(13,432) 두 줄로 실린다. 사업부문·품목·수주일자·납기가
    모두 같고 '기준' 칸 하나만 다르다. 더하면 24,136 백만달러가 되는데 그런
    수주잔고는 없다 - 두 줄은 합이 아니라 가정이 다른 같은 계약이다.
    """
    if len(left) != len(right):
        return False
    differing = [(a, b) for a, b in zip(left, right) if a != b]
    if len(differing) != 1:
        return False
    return all("기준" in value for value in differing[0])


def _total_cell_is_broken(total: dict, detail_rows: list[dict], col: int) -> bool:
    """합계행의 이 칸만 원문 표기가 깨졌는가.

    합계는 세부행 합보다 작을 수 없다(기타·소계가 있으면 더 클 수는 있다). 같은
    행의 다른 칸은 세부합과 맞는데 이 칸만 작으면 그 칸 표기가 깨진 것이다 -
    실측(삼성중공업 2024 사업보고서 20250319000397): 수주잔고 합계가 콤마 대신
    마침표로 '315.350' 이라 315,350억이 315.35억이 됐다. 같은 행의 수주총액
    553,469 와 기납품액 238,119 는 세부합과 정확히 맞는다.
    """
    for column, value in total.items():
        if column == col:
            continue
        detail_sum = sum(e["nums"][column] for e in detail_rows if column in e["nums"])
        if detail_sum and abs(value - detail_sum) <= max(1.0, abs(value) * 0.01):
            return True
    return False


_ROLLFORWARD_STAGE_WORDS = ("기초", "증감", "수익인식", "이월", "취득", "처분",
                            "상각", "설정", "환입", "대체")


def _is_rollforward_header(normalized: list[str]) -> bool:
    """열이 기초→증감→기말 단계로 늘어선 표인가(계약별 상세표가 아니다)."""
    stages = sum(1 for cell in normalized
                 if any(word in cell for word in _ROLLFORWARD_STAGE_WORDS))
    return stages >= 2


def _contract_detail_extract(table: DocumentTable) -> dict | None:
    """계약별 상세표(품목|발주처|...|수주잔고|...)에서 수주잔고 열을 합산한다."""
    default_unit = _table_unit(table)
    rows = table.rows
    for hi, hrow in enumerate(rows):
        norm = [c.replace(" ", "") for c in hrow]
        k = next((i for i, c in enumerate(norm)
                  if any(kw in c for kw in BACKLOG_KEYWORDS)), None)
        if k is None:
            continue
        # 공사 상세표의 첫 열 이름은 회사마다 다르다. 실측(계룡건설 2023
        # 사업보고서 20240319000660)은 '현장명' 이라 이 조건에 안 걸렸고, 표
        # 전체가 단일 값 경로로 새서 115행 중 공사 한 줄(571억)만 읽혔다.
        # 그 표의 원문 합계는 9,397,987 백만원(9.4조)이다.
        if not any(any(word in c for word in _DETAIL_HEADER_WORDS) for c in norm):
            continue
        # 열이 기초→증감→수익인식→이월인 롤포워드 표는 계약별 상세표가 아니다.
        # 실측(한국항공우주 2025 사업보고서 20260318001461): 행이 당기·전기고
        # 열이 단계라, 3항 항등식이 성립할 리 없는데도 검산 실패로 표시돼
        # 단일 값 경로까지 막혔다. 그 표의 당기 이월계약잔액은 6.28조다.
        if _is_rollforward_header(norm):
            continue

        detail_rows: list[dict] = []
        total_rows: list[dict] = []
        for drow in rows[hi + 1:]:
            nums = {i: v for i, cell in enumerate(drow)
                    if (v := _plain_number(cell)) is not None}
            if not nums:
                continue      # 하위 헤더(금액/총액/대손충당금 등)
            # 병합 칸을 편 뒤로는 합계행 라벨이 첫 칸이 아닐 수 있다. 실측
            # (현대건설): 세로 병합된 '구분' 열 때문에 '국내합계'가 둘째 칸에
            # 온다. 숫자가 아닌 칸을 전부 보고 판정한다.
            labels = [cell.strip() for i, cell in enumerate(drow)
                      if i not in nums and cell.strip()]
            entry = {"labels": labels, "nums": nums,
                     "first": labels[0] if labels else ""}
            if _is_total_row(labels):
                total_rows.append(entry)
            elif labels and not labels[0].startswith("*"):
                detail_rows.append(entry)
        if not detail_rows and not total_rows:
            continue

        pre_warnings: list[str] = []
        kept: list[dict] = []
        for entry in detail_rows:
            twin = next((k for k in kept
                         if _is_scenario_twin(k["labels"], entry["labels"])), None)
            if twin is None:
                kept.append(entry)
            else:
                dropped = next(a for a, b in zip(entry["labels"], twin["labels"])
                               if a != b)
                pre_warnings.append(
                    f"표 '{(table.caption or '무제')[:30]}'에서 '{dropped[:30]}' 행은 "
                    "같은 계약을 다른 기준으로 적은 줄이라 합계에 넣지 않았습니다."
                )
        detail_rows = kept

        col, checked = _identity_backlog_column(
            [e["nums"] for e in detail_rows] or [e["nums"] for e in total_rows]
        )
        if col is None:
            # 항등식을 세울 수 있는 표(행마다 숫자 3개 이상)인데 검산이 안 맞으면
            # 추측하지 않는다. 진행률·충당금 열을 금액으로 합산한 것이 예전
            # 오류였다. 이 표는 다른 경로(단일 값 fallback)로도 쓰지 않는다.
            if checked:
                return {"_failed": True, "caption": table.caption[:80]}
            continue

        foreign = _is_foreign_unit(default_unit)
        factor = 1.0 if foreign else {
            "백만원": 0.01, "천원": 0.00001, "억원": 1.0, "원": 0.00000001,
        }.get(default_unit or "억원", 1.0)
        detail_vals = [e["nums"][col] for e in detail_rows if col in e["nums"]]
        total_entries = [e for e in total_rows if col in e["nums"]]
        total_val = _grand_total([e["nums"][col] for e in total_entries])
        broken_total = None
        if total_val is not None and detail_vals and total_val < sum(detail_vals) * 0.99:
            grand = next(e for e in total_entries if e["nums"][col] == total_val)
            if _total_cell_is_broken(grand["nums"], detail_rows, col):
                broken_total, total_val = total_val, None
        if not detail_vals and total_val is None:
            continue

        warnings: list[str] = list(pre_warnings)
        detail_sum = sum(detail_vals)
        if broken_total is not None:
            warnings.append(
                f"표 '{(table.caption or '무제')[:40]}'의 합계행에 적힌 수주잔고"
                f"({_format_value(round(broken_total * factor, 2))}억원)가 세부행 합"
                f"({_format_value(round(detail_sum * factor, 2))}억원)보다 작습니다. "
                "같은 행의 다른 칸은 세부합과 맞아 그 칸 표기가 깨진 것으로 보고 "
                "세부행 합을 사용합니다."
            )
        if total_val is None and total_rows and any(e["nums"] for e in total_rows):
            warnings.append(
                f"표 '{(table.caption or '무제')[:40]}'의 합계행이 여러 개인데 "
                "어느 것이 전체 합계인지 확정하지 못해 세부행을 더했습니다."
            )
        if total_val is not None and detail_vals:
            # 세부행에 '기타'로 뭉친 행이나 소계행이 있으면 합계행과 세부행 합이
            # 다른 게 정상이다(실측 현대건설: 개별 공사 22.7조 + 기타 40.6조 =
            # 합계 69.7조). 그런 행이 없는데도 어긋나면 열을 잘못 읽었다는 뜻이다.
            has_breakdown = len(total_rows) > 1 or any(
                any("기타" in label or "소계" in label for label in e["labels"])
                for e in detail_rows
            )
            if (not has_breakdown
                    and abs(total_val - detail_sum) > max(1.0, total_val * 0.01)):
                warnings.append(
                    f"표 '{(table.caption or '무제')[:40]}'의 합계행"
                    f"({_format_value(round(total_val * factor, 2))}억원)과 세부행 합"
                    f"({_format_value(round(detail_sum * factor, 2))}억원)이 다릅니다. "
                    "원문 명시값인 합계행을 사용합니다."
                )
        raw = total_val if total_val is not None else detail_sum
        if default_unit is None:
            warnings.append(_ASSUMED_UNIT_WARNING)
        if foreign:
            warnings.append(_foreign_unit_warning(default_unit))
        return {
            # 겹침 판정용 - 상세행마다 가장 긴 라벨(대개 공사명)을 들고 간다.
            "_keys": {
                max(e["labels"], key=len).replace(" ", "")
                for e in detail_rows if e["labels"]
            },
            "caption": table.caption[:80],
            "unit": default_unit or "표기 없음(억원 가정)",
            "unit_source": "declared" if default_unit else "assumed",
            "basis": table.basis,
            "currency": "foreign" if foreign else "KRW",
            "source_rows": len(rows),
            "rows_used": 1 if total_val is not None else len(detail_vals),
            "raw_sum": raw,
            "eok_sum": round(raw * factor, 2),
            "method": ("contract_detail(원문 합계행)" if total_val is not None
                       else "contract_detail(항등식 검증 열)"),
            "_max_detail": max((v * factor for v in detail_vals), default=0.0),
            "_warnings": warnings,
        }
    return None


def format_order_backlog_series(
    *,
    corp_code: str,
    report_name: str,
    rcept_no: str,
    series: OrderBacklogSeries,
    sources: list[str] | None = None,
) -> str:
    values = " | ".join(f"{point.period}={_format_value(point.value)}" for point in series.points)
    lines = [f"# {series.metric} 추이 (corp_code={corp_code})", ""]
    if series.unit_source == "assumed":
        lines.append(
            f"⚠️ 단위: {series.unit} **추정** — 원문 표에 단위 표기가 없어 숫자를 "
            f"{series.unit} 그대로 읽었습니다. 원문이 백만원·천원 표기면 실제 값은 "
            "100배·10만배 다릅니다. 아래 rcept_no로 원문 표를 반드시 대조하세요."
        )
    else:
        lines.append(f"단위: {series.unit} (원문 표기 기준)")
    if series.basis:
        lines.append(f"재무기준: {series.basis}재무제표")
    if sources:
        lines.append("출처:")
        lines.extend(f"- {source}" for source in sources)
    else:
        lines.append(f"출처: {report_name} rcept_no={rcept_no}")
    lines.extend(["", f"{series.metric}:", f"  {_period_scope(series.points)} {values}"])
    return "\n".join(lines)


def _period_scope(points: list[OrderBacklogPoint]) -> str:
    """기간 라벨에 월이 섞여 있으면 '[연간]'이라 못 박지 않는다."""
    return "[연간]" if all("." not in p.period for p in points) else "[기간]"


def _extract_from_table(table: DocumentTable, *, limit: int) -> OrderBacklogSeries | None:
    default_unit = _table_unit(table)
    # 외화 표는 _amount_to_eok 가 숫자를 환산하지 않고 그대로 돌려준다. 그 값에 '억원'을
    # 붙이면 12,355 백만달러가 12,355억원이 된다. 단위도 원문 그대로 적는다.
    foreign = _is_foreign_unit(default_unit)
    for index, row in enumerate(table.rows):
        metric = _metric_name(row)
        if metric is None:
            continue
        if _is_opening_balance_row(row):
            continue
        header = _nearest_period_header(table.rows, before=index)
        if header is None:
            continue
        points = _points_from_row(header, row, default_unit=default_unit)
        if points:
            return OrderBacklogSeries(
                metric=metric,
                unit=default_unit if foreign else "억원",
                points=points[-limit:],
                table_caption=table.caption,
                # 셀에도 표에도 단위 표기가 없으면 숫자를 억원으로 "가정"한 것이다.
                # 수주잔고 표는 백만원·천원 표기가 흔해 가정이 틀리면 100배·10만배
                # 어긋난다. 라벨에 확정 단위를 박지 않도록 출처를 같이 들고 간다.
                unit_source=(
                    "declared"
                    if (default_unit or _row_has_inline_unit(row))
                    else "assumed"
                ),
                source_unit=default_unit,
                basis=table.basis,
            )
    return None


def _row_has_inline_unit(row: list[str]) -> bool:
    """셀 자체가 단위를 품고 있는지 (예: '4,100억원', '3.2조')."""
    joined = "".join(row)
    return any(unit in joined for unit in ("조", "억원", "억", "백만원", "천원", "원"))


def _ending_value_from_table(table: DocumentTable) -> tuple[float, list[str]] | None:
    """기말잔액 행의 값과 그 행. 단위 판정에 행이 필요해 같이 돌려준다."""
    if not _table_has_backlog_context(table):
        return None
    if _is_intangible_backlog_table(table):
        return None
    default_unit = _table_unit(table)
    for index, row in enumerate(table.rows):
        if _is_ending_balance_row(row):
            value = _balance_value_from_row(table.rows, index, default_unit=default_unit)
            if value is not None:
                return value, row
    return None


def _fallback_value_from_table(table: DocumentTable) -> tuple[float, list[str]] | None:
    if not _table_has_backlog_context(table):
        return None
    if _is_intangible_backlog_table(table):
        return None
    default_unit = _table_unit(table)
    for index, row in enumerate(table.rows):
        if _is_header_backlog_value_row(table.rows, index):
            value = _balance_value_from_row(table.rows, index, default_unit=default_unit)
            if value is not None:
                return value, row
    return None


def _extract_ending_point_from_table(table: DocumentTable, *, period: str) -> OrderBacklogPoint | None:
    found = _ending_value_from_table(table)
    return OrderBacklogPoint(period=period, value=found[0]) if found else None


def _extract_fallback_point_from_table(table: DocumentTable, *, period: str) -> OrderBacklogPoint | None:
    found = _fallback_value_from_table(table)
    return OrderBacklogPoint(period=period, value=found[0]) if found else None


def _metric_name(row: list[str]) -> str | None:
    joined = " ".join(row)
    for keyword in BACKLOG_KEYWORDS:
        if keyword in joined:
            return keyword
    return None


# '기말잔액' 한 단어로 걸려드는 다른 롤포워드 표들. 수주 키워드가 없으면 뺀다.
_ROLLFORWARD_NOISE = (
    "손실충당금", "대손충당금", "충당부채", "이연법인세", "상각누계액",
    "감가상각", "손상차손", "사용권자산", "리스부채", "주식선택권",
)


def _table_has_backlog_context(table: DocumentTable) -> bool:
    text = table.caption + " " + " ".join(" ".join(row) for row in table.rows[:4])
    normalized = text.replace(" ", "")
    if any(keyword in normalized for keyword in BACKLOG_KEYWORDS):
        return True
    if not any(keyword in normalized for keyword in ENDING_BALANCE_KEYWORDS):
        return False
    # 여기부터는 '기말잔액'만 보고 들어온 표다. 충당금·이연법인세 롤포워드는
    # 기초→증감→기말 모양이 같아 그대로 통과하고, 그 기말 잔액이 수주잔고로
    # 나갈 수 있다. 수주 키워드가 없는 롤포워드 표는 받지 않는다.
    return not any(keyword in normalized for keyword in _ROLLFORWARD_NOISE)


def _is_intangible_backlog_table(table: DocumentTable) -> bool:
    text = " ".join(" ".join(row) for row in table.rows[:3])
    normalized = text.replace(" ", "")
    return "수주잔고" in normalized and any(keyword in normalized for keyword in ("영업권", "고객관계", "무형자산", "상각누계액"))


_OPENING_BALANCE_KEYWORDS = ("기초", "전기이월", "기초잔액")


def _is_opening_balance_row(row: list[str]) -> bool:
    """롤포워드 표의 기초 행. 기초 잔액은 '그 기간의 잔고'가 아니라 직전 기말이다.

    '기초 계약잔액 | 6,616,650 | ...' 을 2025년 잔고로 내보내면 한 해 밀린
    값이 나간다. 기말 행이 같은 표에 있으니 기초 행은 건너뛴다.
    """
    first = (row[0] if row else "").replace(" ", "")
    return any(keyword in first for keyword in _OPENING_BALANCE_KEYWORDS)


def _is_ending_balance_row(row: list[str]) -> bool:
    joined = "".join(row).replace(" ", "")
    if "구분" in joined:
        return False
    if any(keyword in joined for keyword in ENDING_BALANCE_KEYWORDS):
        return True
    return False


def _itemized_backlog_sum(table: DocumentTable, *, default_unit: str | None) -> float | None:
    rows = table.rows
    for index, row in enumerate(rows[:-1]):
        header_text = "".join(row).replace(" ", "")
        if "품목" not in header_text or "수주잔고" not in header_text:
            continue
        sub_header_text = "".join(rows[index + 1]).replace(" ", "")
        if "금액" not in sub_header_text:
            continue
        values: list[float] = []
        for data_row in rows[index + 2 :]:
            if len(data_row) < 2:
                continue
            first = data_row[0].strip()
            if not first or first.startswith("*") or first in {"합계", "비고"}:
                continue
            try:
                values.append(_amount_to_eok(data_row[-1], default_unit=default_unit))
            except ValueError:
                continue
        if values:
            return sum(values)
    return None


def _is_header_backlog_value_row(rows: list[list[str]], row_index: int) -> bool:
    row = rows[row_index]
    if not row:
        return False
    header = _nearest_header(rows, before=row_index)
    if header is None:
        return False
    header_text = "".join(header).replace(" ", "")
    if not any(keyword in header_text for keyword in BACKLOG_KEYWORDS):
        return False
    if any(keyword in "".join(row).replace(" ", "") for keyword in ("상각", "손상", "취득원가", "장부금액")):
        return False
    return any(_looks_numeric(cell) for cell in row[1:])


def _balance_value_from_row(rows: list[list[str]], row_index: int, *, default_unit: str | None) -> float | None:
    """기말잔액 행에서 '전체' 값을 읽는다. 열 위치를 추측하지 않는다.

    예전엔 합계 열을 못 찾으면 맨 오른쪽 칸으로 넘어갔다. 그 자리가 무엇인지는
    표마다 다르다 - 실측(현대엘리베이터 2025 사업보고서 20260318001372):
    연결 계약잔액 표는 합계 열 없이 부문 4개(물품취급장비·건설업·물류·IT)만
    있어서 맨 오른쪽 IT사업부문 914,773천원(9.1억)이 회사 전체 수주잔고로
    나갔다. 실제 합은 2,090,814,579천원(20,908억)이다. 같은 보고서의 별도 표는
    '당기|전기' 구성이라 맨 오른쪽이 전기 - 한 해 묵은 값이 나갔다.

    이제 머리글이 무엇인지 보고 정한다: 합계 열이 있으면 그 열, 기간 축이면
    당기 열, 부문 축이면 전부 더한다. 셋 다 아니면 값을 만들지 않는다.
    """
    row = rows[row_index]
    header = _nearest_header(rows, before=row_index)
    if header is None:
        return None

    index = _preferred_value_index(header, row)
    if index is not None and index < len(row):
        try:
            return _amount_to_eok(row[index], default_unit=default_unit)
        except ValueError:
            pass

    kind = _header_axis_kind(header)
    if kind == "period":
        index = _current_period_index(header)
        if index is not None and index < len(row):
            try:
                return _amount_to_eok(row[index], default_unit=default_unit)
            except ValueError:
                return None
        return None
    if kind == "segment":
        # 합계 열이 없는 부문별 표. 부문은 서로 겹치지 않으니 더한 값이 전체다.
        values = []
        for cell in row[1:]:
            try:
                values.append(_amount_to_eok(cell, default_unit=default_unit))
            except ValueError:
                continue
        return round(sum(values), 6) if values else None
    return None


_PERIOD_HEADER_WORDS = ("당기", "전기", "당반기", "전반기", "당분기", "전분기",
                        "당해", "전년", "기초", "기말")


def _header_axis_kind(header: list[str]) -> str | None:
    """머리글의 값 열들이 기간인지 부문인지. 판정이 안 되면 None."""
    cells = [cell.replace(" ", "") for cell in header[1:] if cell.strip()]
    if not cells:
        return None
    if all(any(word in cell for word in _PERIOD_HEADER_WORDS)
           or _normalize_period(cell) is not None for cell in cells):
        return "period"
    if _is_rollforward_header(cells):
        # 기초·증감·기말이 열로 늘어선 표. 단계를 더하면 아무 뜻이 없다.
        return None
    if all(_plain_number(cell) is None for cell in cells):
        return "segment"
    return None


def _current_period_index(header: list[str]) -> int | None:
    """기간 축 머리글에서 '당기' 열. 연도면 가장 최근 연도."""
    normalized = [cell.replace(" ", "") for cell in header]
    for index, cell in enumerate(normalized):
        if index and any(word in cell for word in ("당기", "당반기", "당분기", "당해")):
            return index
    years = [(period, index) for index, cell in enumerate(normalized)
             if index and (period := _normalize_period(cell)) is not None]
    if years:
        return max(years)[1]
    return 1 if len(header) > 1 else None


def _nearest_header(rows: list[list[str]], *, before: int) -> list[str] | None:
    """이 행의 열 이름이 적힌 머리글 행.

    예전엔 값이 든 행도 머리글로 잡았다 - '기초계약잔액 | 33,937,341 | ...' 은
    계약잔액이라는 낱말이 들어 있어 통과했다. 그 행을 머리글로 보면 열 이름이
    전부 숫자라 어느 열이 합계인지 알 수 없다. 숫자가 든 행은 건너뛴다.
    """
    for index in range(before - 1, -1, -1):
        row = rows[index]
        if not _is_label_row(row):
            continue
        joined = "".join(row).replace(" ", "")
        if "구분" in joined or "합계" in joined or any(keyword in joined for keyword in BACKLOG_KEYWORDS):
            return row
    return None


def _preferred_value_index(header: list[str], row: list[str]) -> int | None:
    """머리글에서 '전체' 열을 찾는다 - 합계 열 또는 잔고 열."""
    normalized = [cell.replace(" ", "") for cell in header]
    for index, cell in enumerate(normalized):
        # '계'·'소 계'처럼 짧게 적은 합계 열도 합계다(_is_total_row 와 같은 판정).
        if index and index < len(row) and _is_total_row([cell]):
            return index
    for keyword in ("합계", "수주잔액", "수주잔고", "기말공사계약잔액", "기말계약잔액",
                    "기말잔액", "이월계약잔액", "이월잔액", "기말계약잔고"):
        for index, cell in enumerate(normalized):
            if keyword in cell and index < len(row):
                return index
    if len(row) == 2 and any(keyword in normalized[-1] for keyword in BACKLOG_KEYWORDS):
        return 1
    return None


def _looks_numeric(value: str) -> bool:
    try:
        _amount_to_eok(value)
    except ValueError:
        return False
    return True


_HEADER_SEARCH_DEPTH = 6


def _nearest_period_header(rows: list[list[str]], *, before: int) -> list[str] | None:
    """이 행의 열을 지배하는 머리글이 '기간 축'일 때만 돌려준다.

    예전엔 위로 끝까지 거슬러 올라가며 네 자리 연도(20xx)가 하나라도 걸리는
    행을 머리글로 삼았다. 실측(현대무벡스)에서 '기초 계약잔액' 행이 102행 위
    주식선택권 '행사가능시점' 행을 머리글로 잡아 부문(IT/물류/합계) 값에
    연도(2025/2026/2027) 라벨이 붙었다.

    규칙: 가장 가까운 머리글 한 줄만 본다. 그게 기간 축이 아니면 더 올라가지
    않고 없음을 돌려준다 - 연도를 지어내지 않는다.
    """
    for index in range(before - 1, max(-1, before - 1 - _HEADER_SEARCH_DEPTH), -1):
        row = rows[index]
        if len(row) < 2:
            continue                     # '(단위: 천원)' 같은 한 칸짜리 주기
        if not _is_label_row(row):
            continue                     # 숫자가 든 데이터 행
        return row if _is_period_axis(row) else None
    return None


def _is_label_row(row: list[str]) -> bool:
    """열 머리글 후보 - 첫 칸을 뺀 나머지에 '값' 숫자가 없는 행.

    연도 칸('2024')도 콤마 없는 숫자라 값처럼 보인다. 기간으로 읽히는 칸은
    라벨로 친다.
    """
    return all(
        _plain_number(cell) is None or _normalize_period(cell) is not None
        for cell in row[1:]
    )


def _is_period_axis(header: list[str]) -> bool:
    """머리글의 값 열들이 실제로 기간을 가리키는가.

    '구분 | IT사업부 | 물류사업부 | 합계' 는 기간 축이 아니다(0/3).
    '구분 | 2022 | 2023 | 2024' 는 기간 축이다(3/3).
    """
    cells = [cell for cell in header[1:] if cell.strip()]
    if not cells:
        return False
    periods = sum(1 for cell in cells if _normalize_period(cell) is not None)
    return periods >= 1 and periods * 2 > len(cells)


def _points_from_row(header: list[str], row: list[str], *, default_unit: str | None) -> list[OrderBacklogPoint]:
    # 칸 수가 다르면 zip 이 열을 한 칸씩 밀어 붙인다. 병합 셀·빈 칸 때문에
    # 흔한 일이고, 밀린 라벨은 숫자가 진짜라서 검산으로 안 걸린다. 맞출 수
    # 없으면 기간을 붙이지 않는다.
    if len(header) != len(row):
        return []
    points: list[OrderBacklogPoint] = []
    for period_cell, value_cell in zip(header, row):
        period = _normalize_period(period_cell)
        if period is None:
            continue
        try:
            value = _amount_to_eok(value_cell, default_unit=default_unit)
        except ValueError:
            continue
        points.append(OrderBacklogPoint(period=period, value=value))
    return points


_PERIOD_RE = re.compile(r"(?P<year>20\d{2})(?:[./-]?(?P<month>0[1-9]|1[0-2]))?")
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def _normalize_period(value: str) -> str | None:
    match = _PERIOD_RE.search(value.replace("년", ""))
    if not match:
        return None
    year = match.group("year")
    month = match.group("month")
    if month:
        return f"{year}.{month}"
    return year


# 외화 표기 -> 정규화 단위. 환산하지 않고 그 단위 그대로 보고한다.
_FOREIGN_UNITS = (
    ("백만달러", "백만달러"), ("백만불", "백만달러"), ("백만US$", "백만달러"),
    ("천달러", "천달러"), ("천불", "천달러"),
    ("USD", "달러"), ("달러", "달러"), ("US$", "달러"),
)


def _table_unit(table: DocumentTable) -> str | None:
    haystack = table.caption + " " + " ".join(" ".join(row) for row in table.rows[:3])
    normalized = haystack.replace(" ", "")
    # 외화가 먼저다 - "백만달러"에서 "원"을 찾으면 안 되고, 실측(삼성바이오로직스)
    # 에서 '(단위: 백만 달러)' 표가 단위 미인식 -> 억원 가정으로 나가
    # 12,355 백만달러(약 18조원)가 12,355억원으로 읽혔다.
    for token, unit in _FOREIGN_UNITS:
        if token in normalized:
            return unit
    for unit in ("백만원", "천원", "억원", "원"):
        if unit in normalized:
            return unit
    return None


def _is_foreign_unit(unit: str | None) -> bool:
    return unit in {"백만달러", "천달러", "달러"}


def _amount_to_eok(value: str, *, default_unit: str | None = None) -> float:
    text = value.strip().replace(" ", "")
    if not text or text in {"-", "데이터없음", "해당사항없음"}:
        raise ValueError("empty amount")
    match = _NUMBER_RE.search(text)
    if not match:
        raise ValueError("amount not found")
    if re.search(r"[가-힣A-Za-z]", text) and not any(unit in text for unit in ("조", "억원", "억", "백만원", "천원", "원")):
        raise ValueError("numeric footnote or label")
    number = float(match.group(0).replace(",", ""))
    if "조" in text:
        return number * 10000
    if "억원" in text or "억" in text:
        return number
    if "백만원" in text:
        return number / 100
    if "천원" in text:
        return number / 100000
    if "원" in text:
        return number / 100000000
    if default_unit == "백만원":
        return number / 100
    if default_unit == "천원":
        return number / 100000
    if default_unit == "억원":
        return number
    if default_unit == "원":
        return number / 100000000
    return number


def _format_value(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"
