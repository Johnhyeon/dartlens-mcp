"""Order backlog extraction from DART document tables."""

from __future__ import annotations

import re
from dataclasses import dataclass

from dartlens._document_tables import DocumentTable


BACKLOG_KEYWORDS = ("수주잔고", "수주잔액", "계약잔액", "계약잔고", "남은 수행의무")
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
    value_unit: str = "억원"    # 외화 표는 원문 단위 그대로(환산하지 않는다)


@dataclass(frozen=True)
class OrderBacklogSeries:
    metric: str
    unit: str
    points: list[OrderBacklogPoint]
    table_caption: str = ""
    unit_source: str = "declared"  # "declared" = 표에 단위 표기 있음 / "assumed" = 없음


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
    for table in tables:
        point = _extract_ending_point_from_table(table, period=period)
        if point is not None:
            return BacklogSnapshot(
                point=point,
                tables=[{
                    "caption": table.caption[:80],
                    "unit": _table_unit(table) or "표기 없음(억원 가정)",
                    "source_rows": len(table.rows),
                    "rows_used": 1,
                    "raw_sum": None,
                    "eok_sum": point.value,
                    "method": "ending_balance",
                }],
                warnings=[],
                max_single_detail=point.value,
            )

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
        units = sorted({i["unit"] for i in extracted if i.get("currency") != "KRW"})
        value_unit = units[0] if units else "억원"
        value = round(sum(i["eok_sum"] for i in extracted), 2)
        max_detail = max(i["_max_detail"] for i in extracted)
        for i in extracted:
            i.pop("_max_detail", None)
        anomalous = value < max_detail * 0.999
        if anomalous:
            warnings.append(
                f"추출된 전체 잔고({_format_value(value)}억원)가 단일 세부 계약 "
                f"최대값({_format_value(round(max_detail, 2))}억원)보다 작습니다. "
                "표 범위·단위·행 선택 오류 가능성이 있어 전체 잔고로 확정하지 않습니다."
            )
        return BacklogSnapshot(
            point=OrderBacklogPoint(period=period, value=value),
            tables=extracted,
            warnings=warnings,
            max_single_detail=round(max_detail, 2),
            anomalous=anomalous,
            value_unit=value_unit,
        )

    # 3) 단일 값 표 - 검산에 실패한 상세표는 여기서도 쓰지 않는다
    for table in tables:
        if hash(tuple(tuple(row) for row in table.rows)) in failed_keys:
            continue
        point = _extract_fallback_point_from_table(table, period=period)
        if point is not None:
            return BacklogSnapshot(
                point=point,
                tables=[{
                    "caption": table.caption[:80],
                    "unit": _table_unit(table) or "표기 없음(억원 가정)",
                    "source_rows": len(table.rows),
                    "rows_used": 1,
                    "raw_sum": None,
                    "eok_sum": point.value,
                    "method": "single_value",
                }],
                warnings=[],
                max_single_detail=point.value,
            )
    return None


_TOTAL_LABELS = {"합계", "총계", "계", "소계"}
_PLAIN_NUMBER_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")


def _plain_number(cell: str) -> float | None:
    """콤마 숫자 셀만 숫자로 읽는다. 날짜('2007-03-09')·라벨은 None."""
    text = (cell or "").strip().replace(" ", "")
    if not text or not _PLAIN_NUMBER_RE.match(text):
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
                    if va <= 0 or vb < 0 or vc < 0:
                        continue
                    if abs(va - (vb + vc)) <= max(2.0, va * 0.005):
                        votes[c] = votes.get(c, 0) + 1
    if checked == 0:
        return None, 0
    if not votes:
        return None, checked
    best, count = max(votes.items(), key=lambda kv: kv[1])
    if count >= max(1, int(checked * 0.7)):
        return best, checked
    return None, checked


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
        if not any(("품목" in c) or ("발주처" in c) or ("구분" in c) for c in norm):
            continue

        detail_rows: list[dict] = []
        total_rows: list[dict] = []
        for drow in rows[hi + 1:]:
            nums = {i: v for i, cell in enumerate(drow)
                    if (v := _plain_number(cell)) is not None}
            if not nums:
                continue      # 하위 헤더(금액/총액/대손충당금 등)
            first = (drow[0] or "").strip()
            entry = {"first": first, "nums": nums}
            if first in _TOTAL_LABELS or first.startswith("합계"):
                total_rows.append(entry)
            elif first and not first.startswith("*"):
                detail_rows.append(entry)
        if not detail_rows and not total_rows:
            continue

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
        total_val = next((e["nums"][col] for e in total_rows if col in e["nums"]), None)
        if not detail_vals and total_val is None:
            continue

        warnings: list[str] = []
        detail_sum = sum(detail_vals)
        if total_val is not None and detail_vals:
            if abs(total_val - detail_sum) > max(1.0, total_val * 0.01):
                warnings.append(
                    f"표 '{(table.caption or '무제')[:40]}'의 합계행"
                    f"({_format_value(round(total_val * factor, 2))}억원)과 세부행 합"
                    f"({_format_value(round(detail_sum * factor, 2))}억원)이 다릅니다. "
                    "원문 명시값인 합계행을 사용합니다."
                )
        raw = total_val if total_val is not None else detail_sum
        if default_unit is None:
            warnings.append(
                "표에 단위 표기가 없어 억원으로 가정했습니다. 원문 대조가 필요합니다."
            )
        if foreign:
            warnings.append(
                f"외화 표({default_unit})입니다. 원화로 환산하지 않고 원문 단위 "
                "그대로 보고합니다 - 억원과 나란히 놓고 비교하면 안 됩니다."
            )
        return {
            "caption": table.caption[:80],
            "unit": default_unit or "표기 없음(억원 가정)",
            "currency": "foreign" if foreign else "KRW",
            "source_rows": len(rows),
            "rows_used": len(detail_vals) if total_val is None else len(detail_vals),
            "raw_sum": raw,
            "eok_sum": round(raw * factor, 2),
            "method": "contract_detail(항등식 검증 열)",
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
    for index, row in enumerate(table.rows):
        metric = _metric_name(row)
        if metric is None:
            continue
        header = _nearest_period_header(table.rows, before=index)
        if header is None:
            continue
        points = _points_from_row(header, row, default_unit=default_unit)
        if points:
            return OrderBacklogSeries(
                metric=metric,
                unit="억원",
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
            )
    return None


def _row_has_inline_unit(row: list[str]) -> bool:
    """셀 자체가 단위를 품고 있는지 (예: '4,100억원', '3.2조')."""
    joined = "".join(row)
    return any(unit in joined for unit in ("조", "억원", "억", "백만원", "천원", "원"))


def _extract_ending_point_from_table(table: DocumentTable, *, period: str) -> OrderBacklogPoint | None:
    if not _table_has_backlog_context(table):
        return None
    if _is_intangible_backlog_table(table):
        return None
    default_unit = _table_unit(table)
    for index, row in enumerate(table.rows):
        if _is_ending_balance_row(row):
            value = _balance_value_from_row(table.rows, index, default_unit=default_unit)
            if value is not None:
                return OrderBacklogPoint(period=period, value=value)
    return None


def _extract_fallback_point_from_table(table: DocumentTable, *, period: str) -> OrderBacklogPoint | None:
    if not _table_has_backlog_context(table):
        return None
    if _is_intangible_backlog_table(table):
        return None
    default_unit = _table_unit(table)
    for index, row in enumerate(table.rows):
        if _is_header_backlog_value_row(table.rows, index):
            value = _balance_value_from_row(table.rows, index, default_unit=default_unit)
            if value is not None:
                return OrderBacklogPoint(period=period, value=value)
    return None


def _metric_name(row: list[str]) -> str | None:
    joined = " ".join(row)
    for keyword in BACKLOG_KEYWORDS:
        if keyword in joined:
            return keyword
    return None


def _table_has_backlog_context(table: DocumentTable) -> bool:
    text = table.caption + " " + " ".join(" ".join(row) for row in table.rows[:4])
    return any(keyword in text.replace(" ", "") for keyword in BACKLOG_KEYWORDS + ENDING_BALANCE_KEYWORDS)


def _is_intangible_backlog_table(table: DocumentTable) -> bool:
    text = " ".join(" ".join(row) for row in table.rows[:3])
    normalized = text.replace(" ", "")
    return "수주잔고" in normalized and any(keyword in normalized for keyword in ("영업권", "고객관계", "무형자산", "상각누계액"))


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
    row = rows[row_index]
    header = _nearest_header(rows, before=row_index)
    preferred_index = _preferred_value_index(header, row) if header is not None else None
    indexes = []
    if preferred_index is not None:
        indexes.append(preferred_index)
    indexes.extend(range(len(row) - 1, 0, -1))
    for index in indexes:
        if index >= len(row):
            continue
        try:
            return _amount_to_eok(row[index], default_unit=default_unit)
        except ValueError:
            continue
    return None


def _nearest_header(rows: list[list[str]], *, before: int) -> list[str] | None:
    for index in range(before - 1, -1, -1):
        row = rows[index]
        joined = "".join(row).replace(" ", "")
        if "구분" in joined or "합계" in joined or any(keyword in joined for keyword in BACKLOG_KEYWORDS):
            return row
    return None


def _preferred_value_index(header: list[str], row: list[str]) -> int | None:
    normalized = [cell.replace(" ", "") for cell in header]
    for keyword in ("합계", "수주잔액", "수주잔고", "기말공사계약잔액", "기말계약잔액", "기말잔액"):
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


def _nearest_period_header(rows: list[list[str]], *, before: int) -> list[str] | None:
    for index in range(before - 1, -1, -1):
        row = rows[index]
        if any(_normalize_period(cell) is not None for cell in row):
            return row
    return None


def _points_from_row(header: list[str], row: list[str], *, default_unit: str | None) -> list[OrderBacklogPoint]:
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
