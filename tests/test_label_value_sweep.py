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


if __name__ == "__main__":
    unittest.main()
