"""Spreadsheet export for scan_earnings_season.

The chat tool returns compact Markdown. This module writes spreadsheet-ready
files with numeric cells and validates the generated shape before returning
paths to the user.
"""

from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from dartlens._cache import EarningsCache
from dartlens._earnings import ScanRow, basis_note, collect_scan_rows
from dartlens._metrics import get_data_dir

EXPORT_HEADERS = [
    "종목명",
    "종목코드",
    "corp_code",
    "공시일",
    "rcept_no",
    "업종",
    "주요제품",
    "매출",
    "매출 YoY",
    "영업이익",
    "OP YoY",
    "OP 마진",
    "순이익",
    "NI YoY",
    "흑자전환 여부",
    "비고",
]

_AMOUNT_UNITS = {"eok", "won"}
_OUTPUT_FORMATS = {"xlsx", "csv", "both"}
_MAX_EXPORT_ROWS = 3000
_XLSX_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass
class ExportFile:
    format: str
    path: Path
    rows: int
    columns: int
    bytes: int


@dataclass
class ExportResult:
    files: list[ExportFile]
    row_count: int
    column_count: int
    universe_size: int
    data_count: int
    missing: int
    amount_unit: str
    warnings: list[str]
    basis_note: str = ""

    def to_markdown(self) -> str:
        unit_label = "억원" if self.amount_unit == "eok" else "원"
        lines = [
            "# 실적 스캔 파일 생성",
            "",
            f"검증: {self.row_count}행 × {self.column_count}열 정상 확인",
            f"금액 단위: {unit_label} 숫자 / 금액 기준: {self.basis_note or '보고서 기준'}",
            f"조회 회사: {self.universe_size} / 데이터 보유: {self.data_count} / 결측: {self.missing}",
            "",
            "| 형식 | 파일 | 크기 | 검증 |",
            "|---|---|---:|---|",
        ]
        for f in self.files:
            lines.append(
                f"| {f.format.upper()} | `{f.path}` | {f.bytes:,} bytes "
                f"| {f.rows}행 × {f.columns}열 |"
            )
        if any(f.format == "csv" for f in self.files):
            lines.append("")
            lines.append("_CSV는 UTF-8 BOM과 표준 따옴표 처리를 적용했습니다. 한국 Excel에서는 XLSX 사용을 권장합니다._")
        for warning in self.warnings:
            lines.append(f"_⚠️ {warning}_")
        return "\n".join(lines)


def _validate_export_args(output_format: str, amount_unit: str, max_rows: int) -> None:
    if output_format not in _OUTPUT_FORMATS:
        raise ValueError(
            f"output_format은 xlsx/csv/both 중 하나여야 합니다 (받음: '{output_format}')."
        )
    if amount_unit not in _AMOUNT_UNITS:
        raise ValueError(f"amount_unit은 eok 또는 won이어야 합니다 (받음: '{amount_unit}').")
    if not isinstance(max_rows, int) or not (1 <= max_rows <= _MAX_EXPORT_ROWS):
        raise ValueError(f"max_rows는 1~{_MAX_EXPORT_ROWS} 정수여야 합니다 (받음: {max_rows}).")


def _amount(value: float | None, amount_unit: str) -> int | float | None:
    if value is None:
        return None
    if amount_unit == "won":
        return int(round(value))
    return round(value / 100_000_000, 1)


def _turnaround(note: str) -> str:
    out = []
    if "영업 흑전" in note:
        out.append("영업흑전")
    if "순익 흑전" in note:
        out.append("순익흑전")
    return ", ".join(out)


def export_rows(rows: list[ScanRow], *, amount_unit: str) -> list[list[object | None]]:
    """Convert ScanRow objects to the 16-column spreadsheet contract."""
    return [
        [
            r.corp_name,
            r.stock_code,
            r.corp_code,
            r.filing_date,
            r.rcept_no,
            r.sector,
            r.product,
            _amount(r.rev, amount_unit),
            r.rev_yoy,
            _amount(r.op, amount_unit),
            r.op_yoy,
            r.op_margin,
            _amount(r.ni, amount_unit),
            r.ni_yoy,
            _turnaround(r.note),
            "" if r.note == "-" else r.note,
        ]
        for r in rows
    ]


def _export_dir() -> Path:
    folder = get_data_dir() / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _safe_part(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_.-]+", "-", value.strip())[:60].strip("-") or "scan"


def _base_path(output_dir: Path, *, period: str, universe: str, fs_div: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"earnings_{_safe_part(period)}_{_safe_part(universe)}_{fs_div}_{stamp}"
    return output_dir / name


def write_csv(path: Path, rows: list[list[object | None]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(EXPORT_HEADERS)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])


def _col_name(idx: int) -> str:
    name = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell_xml(row_idx: int, col_idx: int, value: object | None, *, header: bool = False) -> str:
    ref = f"{_col_name(col_idx)}{row_idx}"
    style = ' s="1"' if header else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = "" if value is None else str(value)
    return f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(text)}</t></is></c>'


def _sheet_xml(rows: list[list[object | None]]) -> str:
    all_rows = [EXPORT_HEADERS, *rows]
    row_xml = []
    for r_idx, row in enumerate(all_rows, 1):
        cells = [
            _cell_xml(r_idx, c_idx, value, header=(r_idx == 1))
            for c_idx, value in enumerate(row, 1)
        ]
        row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    last_ref = f"A1:{_col_name(len(EXPORT_HEADERS))}{len(all_rows)}"
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>
    <col min="1" max="1" width="18" customWidth="1"/>
    <col min="2" max="5" width="14" customWidth="1"/>
    <col min="6" max="7" width="28" customWidth="1"/>
    <col min="8" max="14" width="14" customWidth="1"/>
    <col min="15" max="16" width="18" customWidth="1"/>
  </cols>
  <sheetData>
    {"".join(row_xml)}
  </sheetData>
  <autoFilter ref="{last_ref}"/>
</worksheet>'''


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium9" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>'''


def write_xlsx(path: Path, rows: list[list[object | None]]) -> None:
    files = {
        "[Content_Types].xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>''',
        "xl/workbook.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Earnings Scan" sheetId="1" r:id="rId1"/></sheets>
</workbook>''',
        "xl/_rels/workbook.xml.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": _sheet_xml(rows),
        "xl/styles.xml": _styles_xml(),
        "docProps/core.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>DartLens Earnings Scan</dc:title></cp:coreProperties>''',
        "docProps/app.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>DartLens</Application></Properties>''',
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)


def _read_xlsx_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(xml)
    out = []
    for row in root.findall(".//main:sheetData/main:row", _XLSX_NS):
        values = []
        for cell in row.findall("main:c", _XLSX_NS):
            inline = cell.find("main:is/main:t", _XLSX_NS)
            value = cell.find("main:v", _XLSX_NS)
            values.append("" if inline is not None and inline.text is None else (
                inline.text if inline is not None else (value.text if value is not None else "")
            ))
        out.append(values)
    return out


def validate_csv(path: Path, expected_rows: int) -> tuple[int, int]:
    if not path.exists() or path.stat().st_size <= 0:
        raise ValueError(f"CSV 파일 생성 실패: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] != EXPORT_HEADERS:
        raise ValueError("CSV 헤더 검증 실패.")
    data_rows = rows[1:]
    if len(data_rows) != expected_rows:
        raise ValueError(f"CSV 행 수 검증 실패: 기대 {expected_rows}, 실제 {len(data_rows)}.")
    if any(len(r) != len(EXPORT_HEADERS) for r in rows):
        raise ValueError("CSV 열 수 검증 실패.")
    return len(data_rows), len(EXPORT_HEADERS)


def validate_xlsx(path: Path, expected_rows: int) -> tuple[int, int]:
    if not path.exists() or path.stat().st_size <= 0:
        raise ValueError(f"XLSX 파일 생성 실패: {path}")
    rows = _read_xlsx_rows(path)
    if not rows or rows[0] != EXPORT_HEADERS:
        raise ValueError("XLSX 헤더 검증 실패.")
    data_rows = rows[1:]
    if len(data_rows) != expected_rows:
        raise ValueError(f"XLSX 행 수 검증 실패: 기대 {expected_rows}, 실제 {len(data_rows)}.")
    if any(len(r) != len(EXPORT_HEADERS) for r in rows):
        raise ValueError("XLSX 열 수 검증 실패.")
    return len(data_rows), len(EXPORT_HEADERS)


async def run_export(
    *,
    period: str,
    universe: str = "kospi",
    sort_by: str = "op_yoy",
    direction: str = "desc",
    max_rows: int = 1000,
    output_format: str = "xlsx",
    amount_unit: str = "eok",
    fs_div: str = "CFS",
    output_dir: Path | None = None,
    cache: EarningsCache | None = None,
) -> ExportResult:
    output_format = (output_format or "xlsx").strip().lower()
    amount_unit = (amount_unit or "eok").strip().lower()
    _validate_export_args(output_format, amount_unit, max_rows)

    scan = await collect_scan_rows(
        period=period,
        universe=universe,
        sort_by=sort_by,
        direction=direction,
        limit=max_rows,
        fs_div=fs_div,
        cache=cache,
    )
    rows = export_rows(scan.rows, amount_unit=amount_unit)
    warnings = list(scan.warnings)
    if scan.data_count > len(rows):
        warnings.append(
            f"데이터 보유 {scan.data_count}건 중 {len(rows)}건만 저장했습니다 "
            f"(max_rows={max_rows})."
        )
    output_dir = output_dir or _export_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    base = _base_path(output_dir, period=period, universe=universe, fs_div=fs_div)

    formats = ["xlsx", "csv"] if output_format == "both" else [output_format]
    files: list[ExportFile] = []
    for fmt in formats:
        path = base.with_suffix(f".{fmt}")
        if fmt == "csv":
            write_csv(path, rows)
            row_count, col_count = validate_csv(path, len(rows))
        else:
            write_xlsx(path, rows)
            row_count, col_count = validate_xlsx(path, len(rows))
        files.append(
            ExportFile(
                format=fmt,
                path=path,
                rows=row_count,
                columns=col_count,
                bytes=path.stat().st_size,
            )
        )

    return ExportResult(
        files=files,
        row_count=len(rows),
        column_count=len(EXPORT_HEADERS),
        universe_size=scan.universe_size,
        data_count=scan.data_count,
        missing=scan.missing,
        amount_unit=amount_unit,
        warnings=warnings,
        basis_note=basis_note(scan.reprt_code, scan.rows),
    )
