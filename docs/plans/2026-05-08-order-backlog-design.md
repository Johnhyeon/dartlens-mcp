# Order Backlog Extraction Design

> Scope: dartlens MCP
> Date: 2026-05-08

## Problem

StockLens needs visual order-backlog data for industries where backlog is a key investment signal, such as shipbuilding, defense, construction, and equipment makers. dartlens currently supports disclosure search, disclosure text keyword search, and financial statements, but it does not expose order backlog as structured time-series data.

## Source Policy

DART disclosures are the primary source for chartable numbers. Securities reports can be used later as commentary support, but they should not be the first source for generated charts because licensing and redistribution rules can be ambiguous.

## MVP Tool

Add `get_order_backlog(corp_code, years=3, days=1200)`.

The tool should:

- Find recent regular disclosures for the company.
- Prefer annual reports, then quarterly/semiannual reports if annual reports are unavailable.
- Fetch `document.xml`.
- Preserve table rows from the XML instead of flattening the document into plain text only.
- Search tables containing backlog-related keywords:
  - `수주잔고`
  - `계약잔액`
  - `계약잔고`
  - `남은 수행의무`
  - `수주상황`
- Parse year or period columns and numeric values.
- Return a compact text format that downstream bots can graph.

## Output Contract

The output should be plain text and sourceable:

```text
# 수주잔고 추이 (corp_code=...)

단위: 억원
출처: 2024 사업보고서 rcept_no=...

수주잔고:
  [연간] 2022=32,000 | 2023=41,000 | 2024=56,000
```

If no structured table is found, the tool should say so and suggest keyword search with `get_disclosure_detail`.

## Scope Limits

This MVP should parse simple row/column tables reliably. It does not need to solve every DART table shape. It should be conservative: if the value cannot be parsed as a number or period, skip it instead of guessing.

## Testing

Add network-free unit tests with sample XML:

- A table with period headers and a `수주잔고` row is extracted.
- Numeric Korean units such as `3.2조`, `4,100억원`, and `-` are handled conservatively.
- A disclosure-like list can be scanned and formatted with source metadata.
