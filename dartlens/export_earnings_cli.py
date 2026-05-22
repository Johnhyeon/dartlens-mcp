"""Console CLI for exporting earnings-season scans to spreadsheet files."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from dartlens._earnings_export import run_export

_SORT_CHOICES = ("rev_yoy", "op_yoy", "ni_yoy", "op_margin", "rev", "op", "ni")
_FORMAT_CHOICES = ("xlsx", "csv", "both")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dartlens-export-earnings",
        description="Export a DartLens earnings-season scan to XLSX/CSV.",
    )
    parser.add_argument("--period", required=True, help='Reporting period, e.g. "2026Q1" or "2025".')
    parser.add_argument(
        "--universe",
        default="kospi",
        help='"all", "kospi", "kosdaq", or comma-separated DART corp_code values.',
    )
    parser.add_argument("--sort-by", default="op_yoy", choices=_SORT_CHOICES)
    parser.add_argument("--direction", default="desc", choices=("desc", "asc"))
    parser.add_argument("--max-rows", type=int, default=1000)
    parser.add_argument("--format", "--output-format", dest="output_format", default="xlsx", choices=_FORMAT_CHOICES)
    parser.add_argument("--amount-unit", default="eok", choices=("eok", "won"))
    parser.add_argument("--fs-div", default="CFS", choices=("CFS", "OFS"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = asyncio.run(
            run_export(
                period=args.period,
                universe=args.universe,
                sort_by=args.sort_by,
                direction=args.direction,
                max_rows=args.max_rows,
                output_format=args.output_format,
                amount_unit=args.amount_unit,
                fs_div=args.fs_div,
            )
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(result.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
