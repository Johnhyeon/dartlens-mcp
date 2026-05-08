import unittest

from dartlens._document_tables import extract_document_tables


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


if __name__ == "__main__":
    unittest.main()
