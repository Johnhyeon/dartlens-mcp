# Earnings Export Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a new `export_earnings_scan` MCP tool that writes spreadsheet-ready earnings scan results to `.xlsx`, `.csv`, or both and verifies the generated files.

**Architecture:** Refactor `dartlens._earnings` just enough to expose a reusable data collection result for both Markdown and export use. Add `dartlens._earnings_export` for row conversion, stdlib CSV/XLSX writing, file naming, and validation. Register a thin MCP wrapper in `dartlens.server`.

**Tech Stack:** Python 3.10 stdlib (`csv`, `zipfile`, `xml.etree` or string XML escaping), existing `EarningsCache`, existing DART chunk fetch path, unittest with `AsyncMock`.

---

### Task 1: Add Export Row Tests

**Files:**
- Modify: `tests/test_scan_earnings_season.py`

**Step 1: Write failing tests**

Add tests for:
- `run_export` creates `.csv` with 16 headers and comma-containing product preserved.
- `run_export` creates `.xlsx` with 16 headers and numeric amount/% cells.
- `max_rows` can exceed 100 and limits written rows after sorting.

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scan_earnings_season.py -q`

Expected: FAIL because `dartlens._earnings_export` / `run_export` does not exist.

### Task 2: Expose Reusable Scan Data

**Files:**
- Modify: `dartlens/_earnings.py`

**Step 1: Write minimal data API**

Add `ScanResult` dataclass and `collect_scan_rows(...)` that performs the existing scan work and returns rows plus metadata. Keep Markdown output behavior unchanged by making `run_scan` call `collect_scan_rows`.

**Step 2: Run tests**

Run: `python -m pytest tests/test_scan_earnings_season.py -q`

Expected: existing tests still pass except export tests.

### Task 3: Implement CSV/XLSX Export

**Files:**
- Create: `dartlens/_earnings_export.py`

**Step 1: Implement conversion**

Convert `ScanRow` to the 16-column export contract. Amounts default to numeric 억원. Percent values remain numeric percentage points.

**Step 2: Implement writers**

Use `csv.writer(..., quoting=csv.QUOTE_MINIMAL)` with `utf-8-sig`. Use a minimal `.xlsx` zip writer with workbook metadata, one worksheet, styles for dates/numbers/percent-point columns, and inline strings.

**Step 3: Implement validation**

Re-open CSV with `csv.reader`. Re-open XLSX with `zipfile` and parse `xl/worksheets/sheet1.xml` enough to confirm row/column/header shape.

**Step 4: Run tests**

Run: `python -m pytest tests/test_scan_earnings_season.py -q`

Expected: PASS.

### Task 4: Register MCP Tool

**Files:**
- Modify: `dartlens/server.py`
- Modify: `README.md`

**Step 1: Add wrapper**

Register `export_earnings_scan(period, universe="kospi", sort_by="op_yoy", direction="desc", max_rows=1000, output_format="xlsx", amount_unit="eok", fs_div="CFS")`.

**Step 2: Add docs**

Mention that `.xlsx` is recommended for Korean Excel and CSV is secondary.

**Step 3: Run full test suite**

Run: `python -m pytest -q`

Expected: PASS.

### Task 5: Commit

**Files:**
- Stage only files changed for this feature. Do not stage existing untracked `scan_earnings_season_PROMPT.md`.

**Step 1: Check status**

Run: `git status --short`

**Step 2: Commit**

Run: `git add docs/plans/2026-05-22-earnings-export-design.md docs/plans/2026-05-22-earnings-export-implementation.md dartlens/_earnings.py dartlens/_earnings_export.py dartlens/server.py tests/test_scan_earnings_season.py README.md`

Run: `git commit -m "feat: export earnings scans to spreadsheets"`
