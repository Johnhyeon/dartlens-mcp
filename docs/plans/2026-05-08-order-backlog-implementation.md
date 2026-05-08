# Order Backlog Extraction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a dartlens MCP tool that extracts source-backed order backlog time series from DART disclosure tables.

**Architecture:** Add small pure helpers for XML table extraction and order backlog parsing, then wire them into `dartlens.server.get_order_backlog`. Keep network behavior in the server and keep parsing testable with sample XML fixtures.

**Tech Stack:** Python 3.10+, lxml, existing FastMCP server, unittest.

---

### Task 1: Table Extraction Helper

**Files:**
- Create: `dartlens/_document_tables.py`
- Create: `tests/test_order_backlog.py`

**Steps:**
1. Write a failing unittest that imports `extract_document_tables` and passes sample XML containing a `<table>`.
2. Assert that rows and cleaned cell text are preserved.
3. Implement `DocumentTable` and `extract_document_tables(xml_bytes)`.
4. Run `py -m unittest tests.test_order_backlog`.
5. Commit helper and tests.

### Task 2: Order Backlog Parser

**Files:**
- Create: `dartlens/_order_backlog.py`
- Modify: `tests/test_order_backlog.py`

**Steps:**
1. Write failing tests for `extract_order_backlog_points`.
2. Assert that a table row named `수주잔고` with year columns becomes normalized points.
3. Assert that unsupported values such as `-` are skipped.
4. Implement parsing helpers and formatting helper.
5. Run `py -m unittest tests.test_order_backlog`.
6. Commit parser and tests.

### Task 3: MCP Tool

**Files:**
- Modify: `dartlens/server.py`
- Modify: `README.md`
- Modify: `tests/test_order_backlog.py`

**Steps:**
1. Add a failing async unit test that monkeypatches disclosure list/document fetchers and calls `get_order_backlog`.
2. Implement `get_order_backlog(corp_code, years=3, days=1200)`.
3. Update server instructions and README tool list.
4. Run targeted tests.
5. Run full verification:
   - `py -m unittest tests.test_order_backlog`
   - `py tests/test_validate.py`
   - `py -m compileall dartlens tests`
6. Commit the tool.
