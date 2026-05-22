# Earnings Export Design

## Goal

Add a DartLens MCP export flow that writes `scan_earnings_season` results to `.xlsx`, `.csv`, or both, with row and column validation before returning the file paths.

## Context

The current `scan_earnings_season` tool is optimized for Markdown preview. It limits `top_n` to 100 rows and formats numeric values as Korean text such as `1조 2,345억`, which is useful in chat but poor for spreadsheet sorting. A user request for all 2026 Q1 listed-company earnings produced a 100-row CSV. Korean Excel environments can also split CSV columns incorrectly when `주요제품` contains commas.

## Chosen Approach

Add a separate MCP tool, `export_earnings_scan`, instead of overloading `scan_earnings_season`.

- `scan_earnings_season`: stays as a fast chat/preview table.
- `export_earnings_scan`: creates files under `~/.dartlens/exports`.
- Default output is `.xlsx`; `output_format` can be `xlsx`, `csv`, or `both`.
- CSV is a secondary copy/import format with UTF-8 BOM and RFC4180 quoting.
- XLSX uses a small stdlib writer rather than adding a package dependency.

## Data Shape

The export sheet always has 16 columns:

1. `종목명`
2. `종목코드`
3. `corp_code`
4. `공시일`
5. `rcept_no`
6. `업종`
7. `주요제품`
8. `매출`
9. `매출 YoY`
10. `영업이익`
11. `OP YoY`
12. `OP 마진`
13. `순이익`
14. `NI YoY`
15. `흑자전환 여부`
16. `비고`

`amount_unit` defaults to `eok`, so amounts are numeric 억원 values. Percent columns are numeric percentage points such as `69.2`, not strings with `%`.

## Coverage Policy

`max_rows` defaults to `1000` and may go up to `3000`. The export gathers the whole resolved universe in 100-company DART chunks, sorts all rows, then writes up to `max_rows`. If the data-bearing universe is smaller than `max_rows`, it writes all available rows.

This does not rely on the previous `top_n <= 100` chat limit.

## Validation

After writing, the exporter re-opens the generated file and verifies:

- file exists and has non-zero size
- exactly 16 columns
- header order matches the contract
- row count matches the generated data count

The MCP response includes a compact status such as `1000행 × 16열 정상 확인`.

## Error Handling

Invalid `output_format`, `amount_unit`, or `max_rows` raises a friendly `ValueError`. If no rows are available, the exporter still writes a header-only workbook/CSV and reports `0행 × 16열 정상 확인`.

## Legal / Product Line

This feature only exports DART financial data. It does not add buy/sell recommendations, automated trading, return promises, or brokerage-account collection.
