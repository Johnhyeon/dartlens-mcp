import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class ExportEarningsCliTests(unittest.TestCase):
    def test_cli_passes_arguments_to_export_and_prints_result(self):
        from dartlens import export_earnings_cli

        result = type("Result", (), {"to_markdown": lambda self: "# exported"})()
        run_export = AsyncMock(return_value=result)
        stdout = io.StringIO()

        with patch.object(export_earnings_cli, "run_export", run_export), contextlib.redirect_stdout(stdout):
            code = export_earnings_cli.main(
                [
                    "--period",
                    "2026Q1",
                    "--universe",
                    "all",
                    "--sort-by",
                    "op_yoy",
                    "--direction",
                    "desc",
                    "--max-rows",
                    "3000",
                    "--format",
                    "both",
                    "--amount-unit",
                    "eok",
                    "--fs-div",
                    "CFS",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "# exported\n")
        run_export.assert_awaited_once_with(
            period="2026Q1",
            universe="all",
            sort_by="op_yoy",
            direction="desc",
            max_rows=3000,
            output_format="both",
            amount_unit="eok",
            fs_div="CFS",
        )

    def test_cli_returns_nonzero_and_prints_export_error(self):
        from dartlens import export_earnings_cli

        stderr = io.StringIO()
        with patch.object(export_earnings_cli, "run_export", AsyncMock(side_effect=ValueError("bad period"))), \
             contextlib.redirect_stderr(stderr):
            code = export_earnings_cli.main(["--period", "2026Q4"])

        self.assertEqual(code, 1)
        self.assertIn("bad period", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
