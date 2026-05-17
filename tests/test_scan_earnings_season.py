"""scan_earnings_season 회귀 테스트.

레포 컨벤션(test_order_backlog.py)을 따라 unittest + AsyncMock 사용
(pytest-asyncio 의존성 추가하지 않음). `python -m pytest` /
`python tests/test_scan_earnings_season.py` 둘 다 동작.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import _earnings
from dartlens._cache import EarningsCache
from dartlens._corp_code import CorpEntry
from dartlens._earnings import (
    compute_row,
    extract_accounts,
    parse_period,
    run_scan,
    sort_rows,
)


def _row(corp_code, name, account, ths, frm, fs_div="CFS"):
    return {
        "corp_code": corp_code,
        "corp_name": name,
        "account_nm": account,
        "fs_div": fs_div,
        "sj_div": "IS",
        "thstrm_amount": ths,
        "frmtrm_amount": frm,
    }


# ---------------------------------------------------------------------------
# 1. period 파싱
# ---------------------------------------------------------------------------


class ParsePeriodTests(unittest.TestCase):
    def test_quarter_half_annual(self):
        self.assertEqual(parse_period("2026Q1"), (2026, "11013"))
        self.assertEqual(parse_period("2025H1"), (2025, "11012"))
        self.assertEqual(parse_period("2025Q3"), (2025, "11014"))
        self.assertEqual(parse_period("2024"), (2024, "11011"))
        self.assertEqual(parse_period("2026q1"), (2026, "11013"))

    def test_q4_h2_q2_rejected(self):
        for bad in ("2026Q4", "2026H2", "2026Q2"):
            with self.assertRaises(ValueError):
                parse_period(bad)

    def test_garbage_rejected(self):
        for bad in ("", "26Q1", "2026Q", "abcd", "2026Q9"):
            with self.assertRaises(ValueError):
                parse_period(bad)


# ---------------------------------------------------------------------------
# 2. universe 파싱
# ---------------------------------------------------------------------------


class ResolveUniverseTests(unittest.IsolatedAsyncioTestCase):
    async def test_corp_code_list(self):
        codes, warn = await _earnings.resolve_universe("00126380,00164742")
        self.assertEqual(codes, ["00126380", "00164742"])
        self.assertIsNone(warn)

    async def test_single_corp_code(self):
        codes, warn = await _earnings.resolve_universe("00126380")
        self.assertEqual(codes, ["00126380"])

    async def test_bad_corp_code_rejected(self):
        with self.assertRaises(ValueError):
            await _earnings.resolve_universe("123")  # 8자리 아님

    async def test_all_and_kospi_fallback(self):
        entries = [
            CorpEntry("00126380", "삼성전자", "", "005930", "20250101"),
            CorpEntry("00164742", "SK하이닉스", "", "000660", "20250101"),
            CorpEntry("00999999", "비상장사", "", "", "20250101"),
        ]
        with patch.object(_earnings, "all_listed", AsyncMock(return_value=entries)):
            codes, warn = await _earnings.resolve_universe("all")
            self.assertEqual(set(codes), {"00126380", "00164742"})
            self.assertIsNone(warn)

            codes, warn = await _earnings.resolve_universe("kospi")
            self.assertEqual(set(codes), {"00126380", "00164742"})
            self.assertIsNotNone(warn)
            self.assertIn("폴백", warn)


# ---------------------------------------------------------------------------
# 3. 계정 추출 + YoY (흑전/적전/결측/전년 0)
# ---------------------------------------------------------------------------


class YoYTests(unittest.TestCase):
    def test_normal_yoy(self):
        rows = [
            _row("00126380", "삼성전자", "매출액", "71,200,000,000,000", "60,000,000,000,000"),
            _row("00126380", "삼성전자", "영업이익", "6,600,000,000,000", "640,000,000,000"),
            _row("00126380", "삼성전자", "당기순이익", "7,100,000,000,000", "780,000,000,000"),
        ]
        acc = extract_accounts(rows, "00126380", "CFS")
        r = compute_row("00126380", acc)
        self.assertAlmostEqual(r.rev_yoy, (71.2 - 60) / 60 * 100, places=2)
        self.assertGreater(r.op_yoy, 900)
        self.assertAlmostEqual(r.op_margin, 6.6 / 71.2 * 100, places=2)
        self.assertEqual(r.note, "-")

    def test_turn_to_profit(self):
        rows = [
            _row("00111111", "흑전바이오", "매출액", "14,200,000,000", "9,000,000,000"),
            _row("00111111", "흑전바이오", "영업이익", "800,000,000", "-500,000,000"),
            _row("00111111", "흑전바이오", "당기순이익", "300,000,000", "-200,000,000"),
        ]
        r = compute_row("00111111", extract_accounts(rows, "00111111", "CFS"))
        self.assertIn("영업 흑전", r.note)
        self.assertIn("순익 흑전", r.note)
        # 전년 음수 → YoY 계산은 abs(prev) 기준으로 값이 나옴
        self.assertIsNotNone(r.op_yoy)

    def test_turn_to_loss_and_missing(self):
        rows = [
            _row("00222222", "적전기업", "매출액", "5,000,000,000", "6,000,000,000"),
            _row("00222222", "적전기업", "영업이익", "-100,000,000", "400,000,000"),
            # 당기순이익 행 자체가 없음 → ni 결측
        ]
        r = compute_row("00222222", extract_accounts(rows, "00222222", "CFS"))
        self.assertIn("영업 적전", r.note)
        self.assertIsNone(r.ni)
        self.assertIsNone(r.ni_yoy)

    def test_prev_zero_is_na(self):
        rows = [
            _row("00333333", "전년영", "매출액", "1,000,000,000", "0"),
            _row("00333333", "전년영", "영업이익", "100,000,000", ""),
        ]
        r = compute_row("00333333", extract_accounts(rows, "00333333", "CFS"))
        self.assertIsNone(r.rev_yoy)  # 전년 0
        self.assertIsNone(r.op_yoy)  # 전년 결측

    def test_fs_div_filter(self):
        rows = [
            _row("00444444", "별도만", "매출액", "100", "90", fs_div="OFS"),
        ]
        self.assertIsNone(extract_accounts(rows, "00444444", "CFS"))
        self.assertIsNotNone(extract_accounts(rows, "00444444", "OFS"))


# ---------------------------------------------------------------------------
# 5. 정렬 / top_n
# ---------------------------------------------------------------------------


class SortTests(unittest.TestCase):
    def _mk(self, cc, op_yoy):
        return compute_row(
            cc,
            {
                "corp_name": cc,
                "rev_cur": 100.0,
                "rev_prev": 100.0,
                "op_cur": 10.0,
                "op_prev": None if op_yoy is None else 10.0 / (1 + op_yoy / 100),
                "ni_cur": 5.0,
                "ni_prev": 5.0,
            },
        )

    def test_desc_puts_none_last(self):
        rows = [self._mk("A", 50), self._mk("B", None), self._mk("C", 200)]
        s = sort_rows(rows, "op_yoy", "desc")
        self.assertEqual(s[0].corp_code, "C")
        self.assertEqual(s[1].corp_code, "A")
        self.assertEqual(s[-1].corp_code, "B")  # None 맨 뒤

    def test_asc_puts_none_last(self):
        rows = [self._mk("A", 50), self._mk("B", None), self._mk("C", 200)]
        s = sort_rows(rows, "op_yoy", "asc")
        self.assertEqual(s[0].corp_code, "A")
        self.assertEqual(s[-1].corp_code, "B")  # None 여전히 맨 뒤

    def test_bad_sort_key(self):
        with self.assertRaises(ValueError):
            sort_rows([], "nope", "desc")


# ---------------------------------------------------------------------------
# 4 + 6. 캐시 hit/miss + 마크다운 스모크 (run_scan 통합)
# ---------------------------------------------------------------------------


class RunScanIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _fake_multi_acnt(self):
        async def _impl(corp_codes, bsns_year, reprt_code):
            out = []
            for cc in corp_codes:
                out.append(_row(cc, f"회사{cc}", "매출액", "10,000,000,000", "8,000,000,000"))
                out.append(_row(cc, f"회사{cc}", "영업이익", "2,000,000,000", "1,000,000,000"))
                out.append(_row(cc, f"회사{cc}", "당기순이익", "1,500,000,000", "900,000,000"))
            return out
        return AsyncMock(side_effect=_impl)

    async def test_markdown_and_cache_incremental(self):
        with tempfile.TemporaryDirectory() as td:
            cache = EarningsCache(Path(td) / "earnings.sqlite")
            mock = self._fake_multi_acnt()
            with patch.object(_earnings, "get_multi_acnt", mock), patch.object(
                _earnings, "corp_name_map", AsyncMock(return_value={})
            ):
                # 1차 호출 — API 호출 발생
                out1 = await run_scan(
                    period="2026Q1",
                    universe="00126380,00164742",
                    sort_by="op_yoy",
                    direction="desc",
                    top_n=10,
                    cache=cache,
                )
                calls_after_first = mock.call_count
                self.assertGreater(calls_after_first, 0)

                # 마크다운 스모크
                self.assertIn("# 분기 실적 스캐닝 — 2026Q1", out1)
                self.assertIn("00126380", out1)
                self.assertIn("| 순위 |", out1)
                self.assertIn("OP YoY", out1)
                self.assertIn("데이터 보유: 2", out1)

                # 2차 호출 — 동일 period, 전부 캐시 hit → API 호출 0건 추가
                out2 = await run_scan(
                    period="2026Q1",
                    universe="00126380,00164742",
                    sort_by="op_yoy",
                    direction="desc",
                    top_n=10,
                    cache=cache,
                )
                self.assertEqual(
                    mock.call_count,
                    calls_after_first,
                    "2차 호출은 캐시 hit으로 추가 API 호출이 없어야 함",
                )
                self.assertIn("캐시 hit", out2)
            cache.close()

    async def test_top_n_limits_rows(self):
        with tempfile.TemporaryDirectory() as td:
            cache = EarningsCache(Path(td) / "e.sqlite")
            codes = ",".join(f"{i:08d}" for i in range(1, 6))
            with patch.object(
                _earnings, "get_multi_acnt", self._fake_multi_acnt()
            ), patch.object(_earnings, "corp_name_map", AsyncMock(return_value={})):
                out = await run_scan(
                    period="2024",
                    universe=codes,
                    top_n=3,
                    cache=cache,
                )
            # 데이터 행은 5개지만 top_n=3 → 본문 순위 행 3개
            body_rows = [l for l in out.splitlines() if l.startswith("| ") and "순위" not in l and "---" not in l]
            self.assertEqual(len(body_rows), 3)
            cache.close()


if __name__ == "__main__":
    unittest.main()
