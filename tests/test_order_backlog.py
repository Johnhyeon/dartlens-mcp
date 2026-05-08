import unittest
import zipfile
from io import BytesIO

from dartlens._document_tables import extract_document_tables
from dartlens._document_tables import DocumentTable
from dartlens._order_backlog import extract_order_backlog_series, format_order_backlog_series


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


if __name__ == "__main__":
    unittest.main()
