"""라벨-값 계약 전수 점검(2026-09-17) 회귀 테스트.

숫자·본문에 붙은 이름표(단위·기간·완결성·오류 사유)가 실제 값과 어긋나던 자리들.
전부 오프라인 픽스처로 검증한다 - 실제 DART 를 부르지 않는다.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import _cache
from dartlens import _result_meta as rmeta
from dartlens import server
from dartlens._http import dart_error_from_bytes
from dartlens._safe import DartApiError


def extract_meta(text: str) -> dict:
    payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0].strip()
    return json.loads(payload)


def _dart_error_xml(status: str, message: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<result><status>{status}</status><message>{message}</message></result>"
    ).encode("utf-8")


def _zip_of(xml: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("report.xml", xml.encode("utf-8"))
    return out.getvalue()


class _Licensed(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _cache.clear_cache()
        p = patch("dartlens._safe.is_licensed", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(_cache.clear_cache)


# ---------------------------------------------------------------------------
# 1. document.xml 오류 응답을 공시 본문으로 읽지 않는다
# ---------------------------------------------------------------------------


class DartErrorFromBytesTests(unittest.TestCase):
    def test_status_and_message_are_read(self):
        err = dart_error_from_bytes(_dart_error_xml("020", "사용한도를 초과하였습니다."),
                                    expected="공시 원문 파일")
        self.assertEqual(err.status, "020")
        self.assertEqual(err.message, "사용한도를 초과하였습니다.")

    def test_missing_message_falls_back_to_known_text(self):
        err = dart_error_from_bytes(
            b"<result><status>014</status></result>", expected="공시 원문 파일")
        self.assertEqual(err.status, "014")
        self.assertIn("파일", err.message)

    def test_non_xml_reply_has_no_invented_status(self):
        raw = b"<html><body>maintenance crtfc_key=abcdef1234</body></html>"
        err = dart_error_from_bytes(raw, expected="공시 원문 파일")
        self.assertEqual(err.status, "")
        self.assertIn("공시 원문 파일", err.message)
        self.assertNotIn("abcdef1234", err.message)


class DocumentFetchErrorTests(_Licensed):
    async def test_error_reply_raises_and_is_not_cached(self):
        good = _zip_of("<DOCUMENT>본문</DOCUMENT>")
        get_bytes = AsyncMock(side_effect=[_dart_error_xml("020", "사용한도를 초과하였습니다."), good])
        with patch.object(server, "get_bytes", get_bytes):
            with self.assertRaises(DartApiError) as ctx:
                await server._fetch_document_zip("20260917000001")
            self.assertEqual(ctx.exception.status, "020")
            # 한도가 풀린 뒤 같은 접수번호는 다시 받아야 한다(오류가 캐시되면 안 된다).
            raw = await server._fetch_document_zip("20260917000001")
        self.assertEqual(raw, good)
        self.assertEqual(get_bytes.await_count, 2)

    async def test_rate_limit_is_an_error_not_filing_text(self):
        with patch.object(server, "get_bytes",
                          AsyncMock(return_value=_dart_error_xml("020", "사용한도를 초과하였습니다."))):
            text = await server.get_disclosure_detail(rcept_no="20260917000001")
        self.assertTrue(text.startswith("⚠️ DART API 오류 [020]"), text)
        self.assertNotIn("본문 발췌", text)
        self.assertNotIn(rmeta.MARKER_START, text)

    async def test_key_error_is_reported_with_its_code(self):
        with patch.object(server, "get_bytes",
                          AsyncMock(return_value=_dart_error_xml("010", "등록되지 않은 인증키입니다."))):
            text = await server.get_disclosure_detail(rcept_no="20260917000001", find="배당")
        self.assertTrue(text.startswith("⚠️ DART API 오류 [010]"), text)
        self.assertNotIn("매치 없음", text)

    async def test_no_file_is_empty_result_not_body(self):
        with patch.object(server, "get_bytes",
                          AsyncMock(return_value=_dart_error_xml("014", "파일이 존재하지 않습니다."))):
            text = await server.get_disclosure_detail(rcept_no="20260917000001")
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("본문 발췌", body)
        self.assertIn("원문 파일을 받지 못했습니다", body)
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertIsNone(meta["data_as_of"])

    async def test_order_backlog_stops_on_rate_limit(self):
        reports = [
            {"report_nm": f"사업보고서 ({y}.12)", "rcept_no": f"{y + 1}0320000001",
             "rcept_dt": f"{y + 1}0320"}
            for y in (2025, 2024, 2023)
        ]
        get_bytes = AsyncMock(return_value=_dart_error_xml("020", "사용한도를 초과하였습니다."))
        with patch.object(server, "_fetch_disclosure_list", AsyncMock(return_value={"list": reports})), \
             patch.object(server, "get_bytes", get_bytes):
            text = await server.get_order_backlog("00126380", years=3)
        self.assertTrue(text.startswith("⚠️ DART API 오류 [020]"), text)
        self.assertNotIn("수주잔고 표 없음", text)
        self.assertEqual(get_bytes.await_count, 1)

    async def test_order_backlog_skips_report_without_file(self):
        ok = _zip_of(
            "<DOCUMENT><TABLE>"
            "<TR><TD>(단위:억원)</TD></TR>"
            "<TR><TH>구분</TH><TH>합계</TH></TR>"
            "<TR><TD>기말계약잔액</TD><TD>5,000</TD></TR>"
            "</TABLE></DOCUMENT>"
        )
        reports = [
            {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260320000001", "rcept_dt": "20260320"},
            {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20250320000001", "rcept_dt": "20250320"},
        ]
        docs = {
            "20260320000001": _dart_error_xml("014", "파일이 존재하지 않습니다."),
            "20250320000001": ok,
        }

        async def fake_get_bytes(endpoint, params=None, **kw):
            return docs[params["rcept_no"]]

        with patch.object(server, "_fetch_disclosure_list", AsyncMock(return_value={"list": reports})), \
             patch.object(server, "get_bytes", AsyncMock(side_effect=fake_get_bytes)):
            text = await server.get_order_backlog("00126380", years=2)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("2024=5,000", body)
        self.assertIn("원문 파일 없음(DART 014)", body)


# ---------------------------------------------------------------------------
# 2. 수주잔고 단위 라벨은 원문 표에서 온다
# ---------------------------------------------------------------------------

from dartlens._document_tables import DocumentTable
from dartlens._order_backlog import (
    OrderBacklogSeries,
    extract_order_backlog_series,
    extract_order_backlog_snapshot,
    format_order_backlog_series,
)


class BacklogUnitLabelTests(unittest.TestCase):
    def test_usd_trend_table_is_not_labelled_eok(self):
        table = DocumentTable(
            caption="수주현황 (단위: 백만달러)",
            rows=[["구분", "2023", "2024", "2025"], ["수주잔고", "8,000", "10,704", "12,355"]],
        )
        series = extract_order_backlog_series([table], limit=3)
        self.assertEqual(series.unit, "백만달러")
        self.assertEqual(series.source_unit, "백만달러")
        text = format_order_backlog_series(
            corp_code="00000000", report_name="사업보고서", rcept_no="20260320000001",
            series=series)
        self.assertIn("단위: 백만달러", text)
        self.assertNotIn("억원", text)

    def test_usd_ending_balance_keeps_its_unit(self):
        table = DocumentTable(
            caption="계약잔액 (단위: 백만달러)",
            rows=[["구분", "합계"], ["기말계약잔액", "12,355"]],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertEqual(snap.value_unit, "백만달러")
        self.assertEqual(snap.point.value, 12355.0)
        self.assertTrue(any("외화" in w for w in snap.warnings), snap.warnings)

    def test_unitless_ending_balance_is_assumed_and_warned(self):
        table = DocumentTable(
            caption="계약잔액",
            rows=[["구분", "합계"], ["기말계약잔액", "1,250,000"]],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertEqual(snap.unit_source, "assumed")
        self.assertEqual(snap.tables[0]["unit_source"], "assumed")
        self.assertTrue(any("억원으로 가정" in w for w in snap.warnings), snap.warnings)

    def test_unitless_single_value_is_assumed(self):
        table = DocumentTable(
            caption="수주 현황",
            rows=[["구분", "수주잔고"], ["합계", "1,250,000"]],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.unit_source, "assumed")

    def test_declared_krw_ending_balance_stays_declared(self):
        table = DocumentTable(
            caption="계약잔액 (단위: 백만원)",
            rows=[["구분", "합계"], ["기말계약잔액", "1,250,000"]],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertEqual(snap.value_unit, "억원")
        self.assertEqual(snap.unit_source, "declared")
        self.assertEqual(snap.point.value, 12500.0)
        self.assertEqual(snap.warnings, [])


def _backlog_report(name: str, rcept_no: str, rcept_dt: str) -> dict:
    return {"report_nm": name, "rcept_no": rcept_no, "rcept_dt": rcept_dt}


class _BacklogToolBase(_Licensed):
    async def _run(self, reports, tables_by_rcept, years=3):
        fetched: list[str] = []

        async def fake_zip(rcept_no):
            fetched.append(rcept_no)
            return rcept_no.encode()

        def fake_tables(raw):
            return tables_by_rcept.get(raw.decode(), [])

        with patch.object(server, "_fetch_disclosure_list", AsyncMock(return_value={"list": reports})), \
             patch.object(server, "_fetch_document_zip", AsyncMock(side_effect=fake_zip)), \
             patch.object(server, "extract_document_tables", side_effect=fake_tables):
            text = await server.get_order_backlog("00126380", years=years)
        return text, fetched


class BacklogToolUnitTests(_BacklogToolBase):
    async def test_unitless_snapshots_are_not_called_declared(self):
        unitless = DocumentTable(caption="계약잔액",
                                 rows=[["구분", "합계"], ["기말계약잔액", "1,250,000"]])
        reports = [
            _backlog_report("사업보고서 (2025.12)", "20260320000001", "20260320"),
            _backlog_report("사업보고서 (2024.12)", "20250320000001", "20250320"),
        ]
        text, _ = await self._run(
            reports, {"20260320000001": [unitless], "20250320000001": [unitless]}, years=2)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertNotIn("원문 표기 기준", body)
        self.assertIn("추정", body)

    async def test_usd_snapshots_carry_usd_target_unit(self):
        usd = DocumentTable(caption="계약잔액 (단위: 백만달러)",
                            rows=[["구분", "합계"], ["기말계약잔액", "12,355"]])
        reports = [
            _backlog_report("사업보고서 (2025.12)", "20260320000001", "20260320"),
            _backlog_report("사업보고서 (2024.12)", "20250320000001", "20250320"),
        ]
        text, _ = await self._run(
            reports, {"20260320000001": [usd], "20250320000001": [usd]}, years=2)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("단위: 백만달러", body)
        meta = extract_meta(text)
        self.assertEqual(
            meta["backlog_extraction"]["unit_normalization"]["target_unit"], "백만달러")


# ---------------------------------------------------------------------------
# 7. 수주잔고 기간 라벨은 보고서 기간에서 온다 / 8. 채운 기간은 다시 받지 않는다
# ---------------------------------------------------------------------------


class ReportPeriodLabelTests(unittest.TestCase):
    def test_labels(self):
        cases = {
            "사업보고서 (2025.12)": "2025",
            "[기재정정]사업보고서 (2024.12)": "2024",
            "반기보고서 (2026.06)": "2026.06",
            "분기보고서 (2026.03)": "2026.03",
            "분기보고서 (2025.09)": "2025.09",
            "사업보고서 (2025.03)": "2025.03",   # 3월 결산 - 연말 값이 아니다
        }
        for name, want in cases.items():
            got = server._order_backlog_report_period({"report_nm": name, "rcept_dt": "20260814"})
            self.assertEqual(got, want, name)

    def test_unknown_period_is_not_invented(self):
        self.assertIsNone(server._order_backlog_report_period(
            {"report_nm": "반기보고서", "rcept_dt": "20260814"}))
        # 사업보고서만 접수 연도 - 1 로 추정한다(기존 동작).
        self.assertEqual(server._order_backlog_report_period(
            {"report_nm": "사업보고서", "rcept_dt": "20260320"}), "2025")


class BacklogPeriodToolTests(_BacklogToolBase):
    @staticmethod
    def _table():
        return DocumentTable(caption="계약잔액 (단위: 억원)",
                             rows=[["구분", "합계"], ["기말계약잔액", "5,000"]])

    async def test_half_year_point_is_not_labelled_annual(self):
        reports = [
            _backlog_report("사업보고서 (2025.12)", "20260320000001", "20260320"),
            _backlog_report("사업보고서 (2024.12)", "20250320000001", "20250320"),   # 추출 실패
            _backlog_report("사업보고서 (2023.12)", "20240320000001", "20240320"),
            _backlog_report("반기보고서 (2026.06)", "20260814000001", "20260814"),
        ]
        tables = {r: [self._table()] for r in
                  ("20260320000001", "20240320000001", "20260814000001")}
        text, _ = await self._run(reports, tables, years=3)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertIn("2026.06=5,000", body)
        self.assertNotIn("2026=5,000", body)
        self.assertIn("[기간]", body)
        self.assertNotIn("[연간]", body)

    async def test_already_filled_period_is_not_downloaded_again(self):
        reports = [
            _backlog_report("[기재정정]사업보고서 (2025.12)", "20260801000001", "20260801"),
            _backlog_report("사업보고서 (2025.12)", "20260320000001", "20260320"),
            _backlog_report("사업보고서 (2024.12)", "20250320000001", "20250320"),
        ]
        tables = {r: [self._table()] for r in
                  ("20260801000001", "20260320000001", "20250320000001")}
        text, fetched = await self._run(reports, tables, years=3)
        self.assertEqual(fetched, ["20260801000001", "20250320000001"])
        self.assertIn("2025=5,000", text)

    async def test_period_read_from_correction_is_not_reported_missing(self):
        reports = [
            _backlog_report("사업보고서 (2025.12)", "20260320000001", "20260320"),       # 원본 실패
            _backlog_report("[기재정정]사업보고서 (2025.12)", "20260101000001", "20260101"),
        ]
        # 정렬은 접수일 내림차순이라 원본(03-20)이 정정(01-01, 가짜)보다 먼저 온다.
        tables = {"20260101000001": [self._table()]}
        text, fetched = await self._run(reports, tables, years=1)
        body = text.split(rmeta.MARKER_START)[0]
        self.assertEqual(fetched, ["20260320000001", "20260101000001"])
        self.assertIn("2025=5,000", body)
        self.assertNotIn("빠진 기간", body)
        self.assertEqual(extract_meta(text)["data_completeness"], "complete")


# ---------------------------------------------------------------------------
# 3. 실적 스캔 YoY 는 같은 기준끼리 / 4. 조회 실패는 결측이 아니다
# ---------------------------------------------------------------------------

import tempfile

from dartlens import _earnings
from dartlens._cache import EarningsCache


def _multi_row(corp, account, ths, frm, add=None, frm_add=None, rcept="20260814000001"):
    row = {"corp_code": corp, "rcept_no": rcept, "account_nm": account, "fs_div": "CFS",
           "sj_div": "IS", "thstrm_amount": ths, "frmtrm_amount": frm}
    if add is not None:
        row["thstrm_add_amount"] = add
    if frm_add is not None:
        row["frmtrm_add_amount"] = frm_add
    return row


class _ScanBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.cache = EarningsCache(Path(self._td.name) / "earnings.sqlite")
        self.addCleanup(self._td.cleanup)
        self.addCleanup(self.cache.close)

    async def _collect(self, multi_acnt, universe="00115931", period="2026H1"):
        with patch.object(_earnings, "get_multi_acnt", multi_acnt), \
             patch.object(_earnings, "corp_basic_map", AsyncMock(return_value={})), \
             patch.object(_earnings, "meta_map", AsyncMock(return_value={})):
            return await _earnings.collect_scan_rows(
                period=period, universe=universe, cache=self.cache)


class ScanPrevBasisTests(_ScanBase):
    async def test_missing_prev_cumulative_is_filled_from_prior_year_cumulative(self):
        async def fake(corp_codes, year, reprt_code):
            if int(year) == 2026:   # 전기 누적(frmtrm_add_amount) 없음
                return [_multi_row("00115931", "영업이익", "449", "401", add="862")]
            return [_multi_row("00115931", "영업이익", "401", "380", add="759", frm_add="700",
                               rcept="20250814000001")]

        result = await self._collect(AsyncMock(side_effect=fake))
        row = result.rows[0]
        self.assertEqual(row.op, 862.0)
        self.assertAlmostEqual(row.op_yoy, (862 - 759) / 759 * 100)

    async def test_prior_year_with_other_basis_is_not_used(self):
        async def fake(corp_codes, year, reprt_code):
            if int(year) == 2026:
                return [_multi_row("00115931", "영업이익", "449", "401", add="862")]
            return [_multi_row("00115931", "영업이익", "401", "380",   # 전년은 3개월뿐
                               rcept="20250814000001")]

        result = await self._collect(AsyncMock(side_effect=fake))
        self.assertIsNone(result.rows[0].op_yoy)


class ScanFailureTests(_ScanBase):
    async def test_all_chunks_failing_is_an_error_not_empty_table(self):
        multi = AsyncMock(side_effect=DartApiError("010", "등록되지 않은 인증키입니다."))
        with self.assertRaises(DartApiError) as ctx:
            await self._collect(multi, universe="00115931,00126380")
        self.assertEqual(ctx.exception.status, "010")

    async def test_tool_reports_dart_error_code(self):
        multi = AsyncMock(side_effect=DartApiError("020", "사용한도를 초과하였습니다."))
        with patch("dartlens._safe.is_licensed", return_value=True), \
             patch.object(_earnings, "get_multi_acnt", multi), \
             patch.object(_earnings, "get_earnings_cache", return_value=self.cache):
            text = await server.scan_earnings_season(period="2026H1", universe="00115931")
        self.assertTrue(text.startswith("⚠️ DART API 오류 [020]"), text)

    async def test_partial_failure_is_warned_not_hidden_in_missing(self):
        # 00115931 은 캐시에 있고, 00126380 조회만 한도에 걸린다.
        acc = _earnings.extract_accounts(
            [_multi_row("00115931", "영업이익", "449", "401", add="862", frm_add="759")],
            "00115931", "CFS", "11012")
        self.cache.set_many({EarningsCache.make_key("00115931", 2026, "11012", "CFS"): acc})

        async def fake(corp_codes, year, reprt_code):
            if int(year) == 2026:
                raise DartApiError("020", "사용한도를 초과하였습니다.")
            return []

        result = await self._collect(AsyncMock(side_effect=fake), universe="00115931,00126380")
        self.assertEqual(result.data_count, 1)
        self.assertTrue(
            any("조회 실패로 1개 회사" in w and "020" in w for w in result.warnings),
            result.warnings)


# ---------------------------------------------------------------------------
# 11. "최근 N일"은 한국 날짜 기준
# ---------------------------------------------------------------------------

from datetime import date, datetime, timedelta, timezone

from dartlens import _validate


class KstDateRangeTests(unittest.TestCase):
    def test_us_evening_is_already_tomorrow_in_korea(self):
        pdt = timezone(timedelta(hours=-7))
        now = datetime(2026, 9, 17, 20, 0, tzinfo=pdt)      # = 2026-09-18 12:00 KST
        self.assertEqual(_validate.kst_today(now), date(2026, 9, 18))

    def test_days_to_range_ends_on_korean_today(self):
        with patch.object(_validate, "kst_today", return_value=date(2026, 9, 18)):
            bgn, end = _validate.days_to_range(1)
        self.assertEqual((bgn, end), ("20260917", "20260918"))


# ---------------------------------------------------------------------------
# 5. 정정공시 검색 구간이 잘렸으면 "확인함"이라 적지 않는다
# ---------------------------------------------------------------------------


def _account_row(bsns_year="2020"):
    return {
        "rcept_no": f"{int(bsns_year) + 1}0315000001", "corp_code": "00126380",
        "stock_code": "005930", "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서",
        "account_nm": "매출액", "ord": "1", "currency": "KRW",
        "thstrm_nm": "제 52 기", "thstrm_amount": "1000", "frmtrm_nm": "제 51 기",
        "frmtrm_amount": "900",
    }


class CorrectionWindowTests(unittest.TestCase):
    def test_old_report_window_is_clipped(self):
        bgn, end, clipped = server._correction_window("2020", "11011", today=date(2026, 9, 17))
        self.assertEqual((bgn, end, clipped), (date(2023, 9, 18), date(2026, 9, 17), True))

    def test_recent_report_window_is_whole(self):
        bgn, end, clipped = server._correction_window("2026", "11012", today=date(2026, 9, 17))
        self.assertEqual((bgn, end, clipped), (date(2026, 6, 28), date(2026, 9, 17), False))


class CorrectionRangeToolTests(_Licensed):
    async def _major(self, rows, bsns_year):
        fetch_corr = AsyncMock(return_value=[])
        with patch.object(server, "_fetch_major_accounts", AsyncMock(return_value={"list": rows})), \
             patch.object(server, "_fetch_corrections", fetch_corr), \
             patch.object(server, "kst_today", return_value=date(2026, 9, 17)):
            text = await server.get_major_accounts(
                corp_code="00126380", bsns_year=bsns_year, reprt_code="annual")
        return text, fetch_corr

    async def test_clipped_search_is_not_reported_as_checked(self):
        text, fetch_corr = await self._major([_account_row("2020")], 2020)
        fetch_corr.assert_awaited_once_with("00126380", "20230918", "20260917")
        meta = extract_meta(text)
        state = meta["filing_state"]
        self.assertIs(state["correction_checked"], False)
        self.assertEqual(state["correction_search_range"],
                         {"bgn_de": "2023-09-18", "end_de": "2026-09-17"})
        self.assertTrue(any("2023-09-18" in w and "정정" in w for w in meta["warnings"]),
                        meta["warnings"])

    async def test_whole_window_stays_checked_without_range(self):
        text, _ = await self._major([_account_row("2025")], 2025)
        state = extract_meta(text)["filing_state"]
        self.assertIs(state["correction_checked"], True)
        self.assertNotIn("correction_search_range", state)

    async def test_empty_result_does_not_claim_a_check(self):
        text, fetch_corr = await self._major([], 2025)
        fetch_corr.assert_not_awaited()
        self.assertIs(extract_meta(text)["filing_state"]["correction_checked"], False)


# ---------------------------------------------------------------------------
# 6. 대량보유·임원소유 목록: 잘라 보여주면 complete 가 아니다
# ---------------------------------------------------------------------------


def _holder_rows(n):
    return [
        {"rcept_no": f"202601{(i % 28) + 1:02d}{i:06d}", "rcept_dt": f"202601{(i % 28) + 1:02d}",
         "corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930",
         "repror": f"보고자{i}", "stkqy": "100", "stkrt": "5.0"}
        for i in range(n)
    ]


class HolderListCoverageTests(_Licensed):
    async def _both(self, rows, limit):
        data = {"list": rows}
        with patch.object(server, "_fetch_major_holders", AsyncMock(return_value=data)), \
             patch.object(server, "_fetch_insider_trades", AsyncMock(return_value=data)):
            return [
                extract_meta(await server.get_major_holders(corp_code="00126380", limit=limit)),
                extract_meta(await server.get_insider_trades(corp_code="00126380", limit=limit)),
            ]

    async def test_cut_list_is_partial_with_truthful_counts(self):
        for meta in await self._both(_holder_rows(12), limit=10):
            self.assertEqual(meta["data_completeness"], "partial")
            cov = meta["coverage"]
            self.assertEqual((cov["returned_count"], cov["total_count"]), (10, 12))
            self.assertIs(cov["truncated"], True)
            self.assertIs(cov["coverage_complete"], False)
            self.assertEqual(cov["reason"], "server_cap")

    async def test_whole_list_is_complete(self):
        for meta in await self._both(_holder_rows(3), limit=10):
            self.assertEqual(meta["data_completeness"], "complete")
            self.assertEqual((meta["coverage"]["returned_count"],
                              meta["coverage"]["total_count"]), (3, 3))
            self.assertIs(meta["coverage"]["truncated"], False)

    async def test_empty_list_is_none(self):
        for meta in await self._both([], limit=10):
            self.assertEqual(meta["data_completeness"], "none")
            self.assertEqual(meta["coverage"]["total_count"], 0)


if __name__ == "__main__":
    unittest.main()
