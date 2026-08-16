import unittest
import zipfile
from io import BytesIO
from unittest.mock import AsyncMock, patch

from dartlens import server
from dartlens._document_tables import extract_document_tables
from dartlens._document_tables import DocumentTable
from dartlens._order_backlog import (
    extract_order_backlog_point,
    extract_order_backlog_series,
    format_order_backlog_series,
)


class DocumentTableTests(unittest.TestCase):
    def test_extract_document_tables_preserves_rows_and_cells(self) -> None:
        xml = """
        <DOCUMENT>
          <SECTION>
            <TITLE>수주상황</TITLE>
            <TABLE>
              <TR>
                <TH>구분</TH>
                <TH>2022</TH>
                <TH>2023</TH>
                <TH>2024</TH>
              </TR>
              <TR>
                <TD>수주잔고</TD>
                <TD>3.2조</TD>
                <TD>4.1조</TD>
                <TD>5.6조</TD>
              </TR>
            </TABLE>
          </SECTION>
        </DOCUMENT>
        """.encode("utf-8")

        tables = extract_document_tables(xml)

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].caption, "수주상황")
        self.assertEqual(
            tables[0].rows,
            [
                ["구분", "2022", "2023", "2024"],
                ["수주잔고", "3.2조", "4.1조", "5.6조"],
            ],
        )

    def test_extract_document_tables_accepts_dart_zip_payload(self) -> None:
        xml = """
        <DOCUMENT>
          <TABLE>
            <TR><TH>구분</TH><TH>2024</TH></TR>
            <TR><TD>수주잔고</TD><TD>5.6조</TD></TR>
          </TABLE>
        </DOCUMENT>
        """.encode("utf-8")
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr("report.xml", xml)

        tables = extract_document_tables(payload.getvalue())

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows[1], ["수주잔고", "5.6조"])


class OrderBacklogParserTests(unittest.TestCase):
    def test_extract_order_backlog_series_from_year_columns(self) -> None:
        table = DocumentTable(
            caption="수주상황",
            rows=[
                ["구분", "2022", "2023", "2024"],
                ["수주잔고", "3.2조", "4,100억원", "-"],
                ["신규수주", "1.1조", "2.2조", "3.3조"],
            ],
        )

        series = extract_order_backlog_series([table], limit=3)

        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual(series.metric, "수주잔고")
        self.assertEqual(series.unit, "억원")
        self.assertEqual([(p.period, p.value) for p in series.points], [("2022", 32000.0), ("2023", 4100.0)])

    def test_format_order_backlog_series_includes_source(self) -> None:
        table = DocumentTable(
            caption="수주상황",
            rows=[
                ["구분", "2022", "2023", "2024"],
                ["수주잔고", "3.2조", "4.1조", "5.6조"],
            ],
        )
        series = extract_order_backlog_series([table], limit=3)
        assert series is not None

        text = format_order_backlog_series(
            corp_code="00126380",
            report_name="2024 사업보고서",
            rcept_no="20260318000001",
            series=series,
        )

        self.assertIn("# 수주잔고 추이 (corp_code=00126380)", text)
        self.assertIn("단위: 억원", text)
        self.assertIn("출처: 2024 사업보고서 rcept_no=20260318000001", text)
        self.assertIn("[연간] 2022=32,000 | 2023=41,000 | 2024=56,000", text)

    def test_extract_order_backlog_point_from_contract_balance_total_row(self) -> None:
        table = DocumentTable(
            caption="당기 중 선박 건조 등과 관련하여 수주한 계약 등의 변동내역은 다음과 같습니다.",
            rows=[
                ["(단위:백만원)"],
                ["구분", "조선", "해양플랜트", "기타", "합계"],
                ["기초계약잔액", "33,937,341", "3,629,748", "9,355,847", "46,922,936"],
                ["기말계약잔액", "44,350,193", "2,445,087", "9,586,022", "56,381,302"],
            ],
        )

        point = extract_order_backlog_point([table], period="2025")

        self.assertIsNotNone(point)
        assert point is not None
        self.assertEqual(point.period, "2025")
        self.assertAlmostEqual(point.value, 563813.02)

    def test_extract_order_backlog_point_from_single_backlog_value_table(self) -> None:
        table = DocumentTable(
            caption="(단위 : 억원)",
            rows=[
                ["구분", "수주잔액"],
                ["제24기(2025년)", "262,526"],
            ],
        )

        point = extract_order_backlog_point([table], period="2025")

        self.assertIsNotNone(point)
        assert point is not None
        self.assertEqual(point.value, 262526.0)

    def test_extract_order_backlog_point_ignores_intangible_asset_backlog_columns(self) -> None:
        table = DocumentTable(
            caption="(당기말)",
            rows=[
                ["(단위: 백만원)"],
                ["구분", "영업권", "수주잔고", "고객관계", "합계"],
                ["기초", "352,606", "15,314", "15,441", "394,140"],
                ["상각", "-", "(8,132)", "(3,860)", "(25,118)"],
            ],
        )

        point = extract_order_backlog_point([table], period="2025")

        self.assertIsNone(point)

    def test_extract_order_backlog_point_ignores_generic_ending_balance_tables(self) -> None:
        table = DocumentTable(
            caption="(당기)",
            rows=[
                ["(단위 : 백만원)"],
                ["구분", "기초잔액", "추가", "감가상각비", "기말잔액"],
                ["리스-건물", "35,104", "9,057", "(13,987)", "38,230"],
                ["합계", "133,041", "31,524", "(45,042)", "138,174"],
            ],
        )

        point = extract_order_backlog_point([table], period="2025")

        self.assertIsNone(point)

    def test_extract_order_backlog_point_sums_itemized_order_backlog_amount_column(self) -> None:
        table = DocumentTable(
            caption="(단위 :백만원)",
            rows=[
                ["품목", "수주일자", "납기", "수주총액", "기납품액", "수주잔고"],
                ["수량", "금액", "수량", "금액", "수량", "금액"],
                ["철도A", "2024-01-01", "2028-12-31", "-", "100,000", "-", "10,000", "-", "90,000"],
                ["철도B", "2024-02-01", "2029-12-31", "-", "200,000", "-", "50,000", "-", "150,000"],
            ],
        )

        point = extract_order_backlog_point([table], period="2025")

        self.assertIsNotNone(point)
        assert point is not None
        self.assertEqual(point.value, 2400.0)

    def test_extract_order_backlog_point_prefers_contract_balance_over_itemized_table(self) -> None:
        itemized = DocumentTable(
            caption="(단위 :백만원)",
            rows=[
                ["품목", "수주일자", "납기", "수주총액", "기납품액", "수주잔고"],
                ["수량", "금액", "수량", "금액", "수량", "금액"],
                ["철도A", "2024-01-01", "2028-12-31", "-", "100,000", "-", "10,000", "-", "90,000"],
            ],
        )
        ending = DocumentTable(
            caption="수주한 계약 등의 변동내역",
            rows=[
                ["(단위:백만원)"],
                ["구분", "조선", "합계"],
                ["기말계약잔액", "10,000", "56,000,000"],
            ],
        )

        point = extract_order_backlog_point([itemized, ending], period="2025")

        self.assertIsNotNone(point)
        assert point is not None
        self.assertEqual(point.value, 560000.0)


class OrderBacklogToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_order_backlog_prefers_annual_report_and_formats_series(self) -> None:
        xml = """
        <DOCUMENT>
          <SECTION>
            <TITLE>수주상황</TITLE>
            <TABLE>
              <TR><TH>구분</TH><TH>2022</TH><TH>2023</TH><TH>2024</TH></TR>
              <TR><TD>수주잔고</TD><TD>3.2조</TD><TD>4.1조</TD><TD>5.6조</TD></TR>
            </TABLE>
          </SECTION>
        </DOCUMENT>
        """.encode("utf-8")
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr("report.xml", xml)

        disclosure_list = {
            "list": [
                {"report_nm": "분기보고서 (2025.09)", "rcept_no": "20251114000111", "rcept_dt": "20251114"},
                {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20260318000001", "rcept_dt": "20260318"},
            ]
        }
        fetch_list = AsyncMock(return_value=disclosure_list)
        fetch_doc = AsyncMock(return_value=payload.getvalue())

        with (
            patch("dartlens._safe.is_licensed", return_value=True),
            patch.object(server, "_fetch_disclosure_list", fetch_list),
            patch.object(server, "_fetch_document_zip", fetch_doc),
        ):
            text = await server.get_order_backlog("00126380", years=3, days=1200)

        fetch_doc.assert_awaited_once_with("20260318000001")
        self.assertIn("# 수주잔고 추이 (corp_code=00126380)", text)
        self.assertIn("출처: 사업보고서 (2024.12) rcept_no=20260318000001", text)
        self.assertIn("[연간] 2022=32,000 | 2023=41,000 | 2024=56,000", text)

    async def test_get_order_backlog_builds_trend_from_multiple_annual_reports(self) -> None:
        def payload(amount: str) -> bytes:
            xml = f"""
            <DOCUMENT>
              <TABLE>
                <TR><TD>(단위:백만원)</TD></TR>
                <TR><TH>구분</TH><TH>조선</TH><TH>합계</TH></TR>
                <TR><TD>기말계약잔액</TD><TD>10,000</TD><TD>{amount}</TD></TR>
              </TABLE>
            </DOCUMENT>
            """.encode("utf-8")
            out = BytesIO()
            with zipfile.ZipFile(out, "w") as zf:
                zf.writestr("report.xml", xml)
            return out.getvalue()

        disclosure_list = {
            "list": [
                {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260318000003", "rcept_dt": "20260318"},
                {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20250318000002", "rcept_dt": "20250318"},
                {"report_nm": "사업보고서 (2023.12)", "rcept_no": "20240318000001", "rcept_dt": "20240318"},
            ]
        }
        docs = {
            "20260318000003": payload("56,000,000"),
            "20250318000002": payload("41,000,000"),
            "20240318000001": payload("32,000,000"),
        }
        fetch_list = AsyncMock(return_value=disclosure_list)
        fetch_doc = AsyncMock(side_effect=lambda rcept_no: docs[rcept_no])

        with (
            patch("dartlens._safe.is_licensed", return_value=True),
            patch.object(server, "_fetch_disclosure_list", fetch_list),
            patch.object(server, "_fetch_document_zip", fetch_doc),
        ):
            text = await server.get_order_backlog("00126380", years=3, days=1200)

        self.assertIn("[연간] 2023=320,000 | 2024=410,000 | 2025=560,000", text)
        self.assertIn("출처:", text)
        self.assertIn("2025: 사업보고서 (2025.12) rcept_no=20260318000003", text)


if __name__ == "__main__":
    unittest.main()


class OrderBacklogUnitProvenanceTests(unittest.TestCase):
    """단위 표기가 없는 표를 억원으로 '확정' 라벨링하던 회귀 방지.

    수주잔고 표는 백만원·천원 표기가 흔하다. 단위를 못 찾았는데 '단위: 억원'이라고
    박으면 100배·10만배 틀린 숫자가 확정 사실처럼 나간다.
    """

    NO_UNIT = DocumentTable(
        caption="가. 수주 현황",
        rows=[["구분", "2024", "2025", "2026"],
              ["수주잔고", "1,250,000", "1,480,000", "1,610,000"]],
    )
    TABLE_UNIT = DocumentTable(
        caption="(단위: 백만원)",
        rows=[["구분", "2024", "2025"], ["수주잔고", "1,250,000", "1,480,000"]],
    )
    CELL_UNIT = DocumentTable(
        caption="수주현황",
        rows=[["구분", "2024", "2025"], ["수주잔고", "3.2조", "4,100억원"]],
    )

    def _fmt(self, table):
        series = extract_order_backlog_series([table], limit=3)
        self.assertIsNotNone(series)
        return series, format_order_backlog_series(
            corp_code="00000000", report_name="사업보고서",
            rcept_no="20260101000001", series=series,
        )

    def test_missing_unit_is_flagged_as_assumed(self):
        series, text = self._fmt(self.NO_UNIT)
        self.assertEqual(series.unit_source, "assumed")
        self.assertIn("추정", text)
        self.assertIn("원문 표를 반드시 대조", text)

    def test_table_declared_unit_is_not_flagged(self):
        series, text = self._fmt(self.TABLE_UNIT)
        self.assertEqual(series.unit_source, "declared")
        self.assertNotIn("추정", text)
        self.assertIn("원문 표기 기준", text)
        self.assertIn("2024=12,500", text)  # 1,250,000 백만원 = 12,500억

    def test_inline_cell_unit_is_not_flagged(self):
        series, text = self._fmt(self.CELL_UNIT)
        self.assertEqual(series.unit_source, "declared")
        self.assertNotIn("추정", text)
        self.assertIn("2024=32,000", text)  # 3.2조 = 32,000억

    def test_monthly_periods_are_not_labelled_annual(self):
        table = DocumentTable(
            caption="(단위: 억원)",
            rows=[["구분", "2025.12", "2026.06"], ["수주잔고", "1,200", "1,400"]],
        )
        _, text = self._fmt(table)
        self.assertIn("[기간]", text)
        self.assertNotIn("[연간]", text)
