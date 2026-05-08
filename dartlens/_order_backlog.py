"""Order backlog extraction from DART document tables."""

from __future__ import annotations

import re
from dataclasses import dataclass

from dartlens._document_tables import DocumentTable


BACKLOG_KEYWORDS = ("수주잔고", "계약잔액", "계약잔고", "남은 수행의무")


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


def extract_order_backlog_series(tables: list[DocumentTable], *, limit: int = 3) -> OrderBacklogSeries | None:
    for table in tables:
        series = _extract_from_table(table, limit=limit)
        if series is not None:
            return series
    return None


def format_order_backlog_series(
    *,
    corp_code: str,
    report_name: str,
    rcept_no: str,
    series: OrderBacklogSeries,
) -> str:
    values = " | ".join(f"{point.period}={_format_value(point.value)}" for point in series.points)
    return "\n".join(
        [
            f"# {series.metric} 추이 (corp_code={corp_code})",
            "",
            f"단위: {series.unit}",
            f"출처: {report_name} rcept_no={rcept_no}",
            "",
            f"{series.metric}:",
            f"  [연간] {values}",
        ]
    )


def _extract_from_table(table: DocumentTable, *, limit: int) -> OrderBacklogSeries | None:
    for index, row in enumerate(table.rows):
        metric = _metric_name(row)
        if metric is None:
            continue
        header = _nearest_period_header(table.rows, before=index)
        if header is None:
            continue
        points = _points_from_row(header, row)
        if points:
            return OrderBacklogSeries(
                metric=metric,
                unit="억원",
                points=points[-limit:],
                table_caption=table.caption,
            )
    return None


def _metric_name(row: list[str]) -> str | None:
    joined = " ".join(row)
    for keyword in BACKLOG_KEYWORDS:
        if keyword in joined:
            return keyword
    return None


def _nearest_period_header(rows: list[list[str]], *, before: int) -> list[str] | None:
    for index in range(before - 1, -1, -1):
        row = rows[index]
        if any(_normalize_period(cell) is not None for cell in row):
            return row
    return None


def _points_from_row(header: list[str], row: list[str]) -> list[OrderBacklogPoint]:
    points: list[OrderBacklogPoint] = []
    for period_cell, value_cell in zip(header, row):
        period = _normalize_period(period_cell)
        if period is None:
            continue
        try:
            value = _amount_to_eok(value_cell)
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


def _amount_to_eok(value: str) -> float:
    text = value.strip().replace(" ", "")
    if not text or text in {"-", "데이터없음", "해당사항없음"}:
        raise ValueError("empty amount")
    match = _NUMBER_RE.search(text)
    if not match:
        raise ValueError("amount not found")
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
    return number


def _format_value(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.1f}"
