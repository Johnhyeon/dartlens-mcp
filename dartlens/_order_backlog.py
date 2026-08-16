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
    for table in tables:
        point = _extract_ending_point_from_table(table, period=period)
        if point is not None:
            return point
    for table in tables:
        point = _extract_fallback_point_from_table(table, period=period)
        if point is not None:
            return point
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
    itemized_value = _itemized_backlog_sum(table, default_unit=default_unit)
    if itemized_value is not None:
        return OrderBacklogPoint(period=period, value=itemized_value)
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


def _table_unit(table: DocumentTable) -> str | None:
    haystack = table.caption + " " + " ".join(" ".join(row) for row in table.rows[:3])
    normalized = haystack.replace(" ", "")
    for unit in ("백만원", "천원", "억원", "원"):
        if unit in normalized:
            return unit
    return None


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
