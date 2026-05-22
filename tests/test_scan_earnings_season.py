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
from dartlens._market import parse_corp_list
from dartlens._earnings import (
    compute_row,
    extract_accounts,
    parse_period,
    run_scan,
    sort_rows,
)


def _row(corp_code, name, account, ths, frm, fs_div="CFS", rcept_no="20260515000001"):
    return {
        "corp_code": corp_code,
        "corp_name": name,
        "rcept_no": rcept_no,
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

    async def test_all_returns_all_listed(self):
        entries = [
            CorpEntry("00126380", "삼성전자", "", "005930", "20250101"),
            CorpEntry("00164742", "현대차", "", "005380", "20250101"),
            CorpEntry("00999999", "비상장사", "", "", "20250101"),
        ]
        with patch.object(_earnings, "all_listed", AsyncMock(return_value=entries)), \
             patch.object(_earnings, "market_stock_codes", AsyncMock(return_value={"005930"})):
            codes, warn = await _earnings.resolve_universe("all")
            self.assertEqual(set(codes), {"00126380", "00164742"})  # 비상장 제외
            self.assertIsNone(warn)  # all은 시장맵 무시


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

    def test_implausible_value_flagged_not_clamped(self):
        rows = [
            _row("00415628", "세진중공업", "매출액", "87,243,092,705,000,000", "98,495,969,479,000,000"),
            _row("00415628", "세진중공업", "영업이익", "13,638,834,006,000,000", "17,800,868,936,000,000"),
        ]
        r = compute_row("00415628", extract_accounts(rows, "00415628", "CFS"))
        self.assertTrue(r.flagged)
        self.assertIn("⚠원본확인", r.note)
        # 값은 조작/클램프하지 않고 그대로 보존
        self.assertEqual(r.rev, 87243092705000000.0)

    def test_normal_value_not_flagged(self):
        rows = [
            _row("00126380", "삼성전자", "매출액", "133,873,444,000,000", "79,140,503,000,000"),
        ]
        r = compute_row("00126380", extract_accounts(rows, "00126380", "CFS"))
        self.assertFalse(r.flagged)
        self.assertNotIn("⚠", r.note)

    def test_fs_div_filter(self):
        rows = [
            _row("00444444", "별도만", "매출액", "100", "90", fs_div="OFS"),
        ]
        self.assertIsNone(extract_accounts(rows, "00444444", "CFS"))
        self.assertIsNotNone(extract_accounts(rows, "00444444", "OFS"))

    def test_receipt_no_becomes_filing_date(self):
        rows = [
            _row(
                "00126380",
                "삼성전자",
                "매출액",
                "133,873,444,000,000",
                "79,140,503,000,000",
                rcept_no="20260430001234",
            ),
        ]
        acc = extract_accounts(rows, "00126380", "CFS")
        self.assertEqual(acc["rcept_no"], "20260430001234")
        self.assertEqual(acc["filing_date"], "2026-04-30")
        r = compute_row("00126380", acc)
        self.assertEqual(r.rcept_no, "20260430001234")
        self.assertEqual(r.filing_date, "2026-04-30")


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
                _earnings, "corp_basic_map", AsyncMock(return_value={})
            ), patch.object(_earnings, "meta_map", AsyncMock(return_value={})):
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
                self.assertIn("공시일", out1)
                self.assertIn("2026-05-15", out1)
                self.assertIn("rcept_no=20260515000001", out1)
                self.assertIn("OP YoY", out1)
                self.assertIn("데이터 보유: 2", out1)
                self.assertIn("실적 기간", out1)
                self.assertIn("공시 접수일", out1)
                self.assertIn("주가·수급 반응", out1)

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

    async def test_legacy_cache_without_receipt_metadata_is_refetched(self):
        with tempfile.TemporaryDirectory() as td:
            cache = EarningsCache(Path(td) / "legacy.sqlite")
            old_payload = {
                "corp_name": "구캐시",
                "rev_cur": 10_000_000_000.0,
                "rev_prev": 8_000_000_000.0,
                "op_cur": 2_000_000_000.0,
                "op_prev": 1_000_000_000.0,
                "ni_cur": 1_500_000_000.0,
                "ni_prev": 900_000_000.0,
            }
            cache.set_many(
                {
                    EarningsCache.make_key("00126380", 2026, "11013", "CFS"): old_payload,
                    EarningsCache.make_key("00126380", 2025, "11013", "CFS"): old_payload,
                }
            )
            mock = self._fake_multi_acnt()
            with patch.object(_earnings, "get_multi_acnt", mock), patch.object(
                _earnings, "corp_basic_map", AsyncMock(return_value={})
            ), patch.object(_earnings, "meta_map", AsyncMock(return_value={})):
                out = await run_scan(
                    period="2026Q1",
                    universe="00126380",
                    sort_by="op_yoy",
                    direction="desc",
                    top_n=1,
                    cache=cache,
                )
            self.assertGreater(mock.call_count, 0)
            self.assertIn("2026-05-15", out)
            self.assertIn("rcept_no=20260515000001", out)
            self.assertNotIn("| N/A |", out)
            cache.close()

    async def test_top_n_limits_rows(self):
        with tempfile.TemporaryDirectory() as td:
            cache = EarningsCache(Path(td) / "e.sqlite")
            codes = ",".join(f"{i:08d}" for i in range(1, 6))
            with patch.object(
                _earnings, "get_multi_acnt", self._fake_multi_acnt()
            ), patch.object(
                _earnings, "corp_basic_map", AsyncMock(return_value={})
            ), patch.object(_earnings, "meta_map", AsyncMock(return_value={})):
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


# ---------------------------------------------------------------------------
# KRX 시장구분 — 파서 + universe 필터 + 폴백
# ---------------------------------------------------------------------------

_KRX_HTML = """
<html><body><table class="bbs_tb">
<tr><th>회사명</th><th>시장구분</th><th>종목코드</th><th>업종</th><th>주요제품</th></tr>
<tr><td>삼성전자</td><td>유가</td><td>005930</td><td>통신 및 방송 장비 제조업</td><td>반도체, 디스플레이</td></tr>
<tr><td>리딩제로</td><td>유가</td><td>60310</td><td>소프트웨어 개발 및 공급업</td><td>SW</td></tr>
</table></body></html>
"""


class KrxParserTests(unittest.TestCase):
    def test_parse_corp_list_extracts_meta_and_zfills(self):
        m = parse_corp_list(_KRX_HTML, "KOSPI")
        self.assertEqual(m["005930"]["market"], "KOSPI")
        self.assertEqual(m["005930"]["sector"], "통신 및 방송 장비 제조업")
        self.assertEqual(m["005930"]["product"], "반도체, 디스플레이")
        self.assertIn("060310", m)  # 5자리 → zfill(6)
        self.assertEqual(len(m), 2)

    def test_parse_corp_list_empty_on_garbage(self):
        self.assertEqual(parse_corp_list("<html><body>no table</body></html>", "KOSPI"), {})


class UniverseMarketFilterTests(unittest.IsolatedAsyncioTestCase):
    def _entries(self):
        return [
            CorpEntry("00126380", "삼성전자", "", "005930", "20250101"),   # KOSPI
            CorpEntry("00164779", "SK하이닉스", "", "000660", "20250101"),  # KOSDAQ(가정)
            CorpEntry("00999999", "코넥스사", "", "111111", "20250101"),     # 미분류
        ]

    async def test_kospi_filters_by_krx_map(self):
        with patch.object(_earnings, "all_listed", AsyncMock(return_value=self._entries())), \
             patch.object(_earnings, "market_stock_codes", AsyncMock(return_value={"005930"})):
            codes, warn = await _earnings.resolve_universe("kospi")
            self.assertEqual(codes, ["00126380"])
            self.assertIsNone(warn)

    async def test_empty_krx_map_falls_back_with_warning(self):
        with patch.object(_earnings, "all_listed", AsyncMock(return_value=self._entries())), \
             patch.object(_earnings, "market_stock_codes", AsyncMock(return_value=set())):
            codes, warn = await _earnings.resolve_universe("kosdaq")
            self.assertEqual(set(codes), {"00126380", "00164779", "00999999"})
            self.assertIsNotNone(warn)
            self.assertIn("폴백", warn)

    async def test_all_ignores_market_map(self):
        with patch.object(_earnings, "all_listed", AsyncMock(return_value=self._entries())), \
             patch.object(_earnings, "market_stock_codes", AsyncMock(return_value={"005930"})):
            codes, warn = await _earnings.resolve_universe("all")
            self.assertEqual(set(codes), {"00126380", "00164779", "00999999"})
            self.assertIsNone(warn)


# ---------------------------------------------------------------------------
# 섹터 집계 (group_by="sector")
# ---------------------------------------------------------------------------


class SectorAggregateTests(unittest.TestCase):
    def _row(self, sector, op_yoy, note="-"):
        r = compute_row(
            "0" * 8,
            {
                "corp_name": "x",
                "rev_cur": 100.0,
                "rev_prev": 80.0,
                "op_cur": 10.0,
                "op_prev": (10.0 / (1 + op_yoy / 100)) if op_yoy is not None else None,
                "ni_cur": 5.0,
                "ni_prev": 4.0,
            },
        )
        r.sector = sector
        if note != "-":
            r.note = note
        return r

    def test_min_firms_threshold_and_median(self):
        rows = (
            [self._row("정유", 100), self._row("정유", 300), self._row("정유", 200)]
            + [self._row("소형업종", 999), self._row("소형업종", 5)]  # 2개 → 제외
        )
        aggs = {a.sector: a for a in _earnings.aggregate_sectors(rows)}
        self.assertIn("정유", aggs)
        self.assertNotIn("소형업종", aggs)  # 3개 미만 제외
        self.assertEqual(aggs["정유"].n, 3)
        self.assertAlmostEqual(aggs["정유"].op_yoy_median, 200, places=4)  # 중앙값

    def test_turnaround_ratio_counts_흑전(self):
        rows = [
            self._row("화학", 50, note="영업 흑전"),
            self._row("화학", 60, note="순익 흑전"),
            self._row("화학", 70, note="-"),
            self._row("화학", 80, note="-"),
        ]
        a = _earnings.aggregate_sectors(rows)[0]
        self.assertEqual(a.turnaround, 2)
        self.assertAlmostEqual(a.turn_ratio, 0.5)

    def test_op_inc_ratio_distinct_from_turnaround(self):
        # 4사 전부 흑자, 작년比 영익 증가 3사 / 감소 1사 → 영익↑비율 0.75.
        # 적자→흑자는 0건이므로 흑전비율 0 — 두 지표가 분리됨을 검증.
        rows = [
            self._row("반도체", 50),
            self._row("반도체", 20),
            self._row("반도체", 10),
            self._row("반도체", -30),  # 감소(여전히 흑자, 흑전 아님)
        ]
        a = _earnings.aggregate_sectors(rows)[0]
        self.assertAlmostEqual(a.op_inc_ratio, 0.75)
        self.assertEqual(a.turnaround, 0)
        self.assertAlmostEqual(a.turn_ratio, 0.0)

    def test_op_inc_ratio_none_when_no_yoy(self):
        # 전년 결측이면 op_yoy 산출 불가 → 분모 0 → None (0%로 오인 금지)
        rows = [self._row("정유", None), self._row("정유", None), self._row("정유", None)]
        a = _earnings.aggregate_sectors(rows)[0]
        self.assertIsNone(a.op_inc_ratio)

    def test_median_immune_to_implausible_outlier(self):
        # 87,243조급 이상치가 섞여도 중앙값은 정상
        rows = [
            self._row("정유", 10),
            self._row("정유", 20),
            self._row("정유", 30),
        ]
        rows.append(self._row("정유", 163349))  # 전년바닥 폭증 노이즈
        a = _earnings.aggregate_sectors(rows)[0]
        self.assertAlmostEqual(a.op_yoy_median, 25, places=4)  # (20+30)/2, 이상치 무영향


class SectorMarkdownSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_by_sector_output(self):
        def _impl(corp_codes, bsns_year, reprt_code):
            out = []
            for cc in corp_codes:
                out.append(_row(cc, f"회사{cc}", "매출액", "10,000,000,000", "8,000,000,000"))
                out.append(_row(cc, f"회사{cc}", "영업이익", "2,000,000,000", "1,000,000,000"))
                out.append(_row(cc, f"회사{cc}", "당기순이익", "1,500,000,000", "900,000,000"))
            return out

        codes = ",".join(f"{i:08d}" for i in range(1, 5))
        basic = {f"{i:08d}": (f"회사{i}", f"00000{i}") for i in range(1, 5)}
        metas = {
            f"00000{i}": {"market": "KOSPI", "sector": "석유 정제품 제조업", "product": "정유"}
            for i in range(1, 5)
        }
        with tempfile.TemporaryDirectory() as td:
            cache = EarningsCache(Path(td) / "e.sqlite")
            with patch.object(_earnings, "get_multi_acnt", AsyncMock(side_effect=_impl)), \
                 patch.object(_earnings, "corp_basic_map", AsyncMock(return_value=basic)), \
                 patch.object(_earnings, "meta_map", AsyncMock(return_value=metas)):
                out = await run_scan(
                    period="2026Q1", universe=codes, group_by="sector", cache=cache
                )
            cache.close()
        self.assertIn("# 섹터 실적 스캐닝 — 2026Q1", out)
        self.assertIn("석유 정제품 제조업", out)
        self.assertIn("영익↑비율", out)
        self.assertIn("흑전비율", out)
        self.assertIn("정렬: 영익증가비율", out)
        self.assertIn("중앙값", out)

    async def test_invalid_group_by_rejected(self):
        with self.assertRaises(ValueError):
            await run_scan(period="2024", universe="00000001", group_by="bogus")


if __name__ == "__main__":
    unittest.main()
