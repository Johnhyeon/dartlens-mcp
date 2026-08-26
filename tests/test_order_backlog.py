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
# ---------------------------------------------------------------------------
# DL-01. 원문 범위·단위·합계 검증
#
# 실측(두산에너빌리티 2025 사업보고서 20260320001246): 계약별 상세표의 마지막
# 열은 진행률(%)인데 예전 파서가 그 열을 금액으로 합산해 "수주잔고 261.6억원"을
# 만들었다. 같은 보고서의 단일 계약(체코, 4.8조원)보다 작은 값이 전체 잔고로
# 나간 것이다. 수주잔고 열은 행 항등식(수주총액 = 기납품액 + 수주잔고)으로
# 자가검증하며 찾는다.
# ---------------------------------------------------------------------------

from dartlens._order_backlog import extract_order_backlog_snapshot


def _doosan_table(caption="(단위 : 백만원, %)"):
    """두산 2025 사업보고서 표3 축약본 - 마지막 열이 진행률이다."""
    return DocumentTable(
        caption=caption,
        rows=[
            ["품목", "발주처", "계약일", "공사기한", "수주총액", "기납품액", "수주잔고", "진행률"],
            ["금액", "금액", "금액", "총액", "대손충당금", "총액", "대손충당금"],
            ["새울 1,2호기", "한수원", "2007-03-09", "2019-08-30",
             "530,600", "529,895", "705", "99.87"],
            ["새울 3,4호기", "한수원", "2015-06-12", "2026-10-31",
             "1,004,518", "991,551", "12,967", "98.71"],
        ],
    )


def _doosan_table2():
    """표5 축약본 - 다른 사업부문."""
    return DocumentTable(
        caption="(단위 : 백만원, %)",
        rows=[
            ["품목", "발주처", "계약일", "공사기한", "수주총액", "기납품액", "수주잔고", "진행률"],
            ["금액", "금액", "금액", "총액", "대손충당금", "총액", "대손충당금"],
            ["체코 Dukovany", "한수원", "2025-12-15", "2038-04-18",
             "4,805,196", "2,670", "4,802,526", "0.06"],
            ["신한울 3,4호기", "한수원", "2023-03-29", "2033-10-31",
             "2,341,601", "751,881", "1,589,720", "32.11"],
        ],
    )


class ContractDetailColumnTests(unittest.TestCase):
    """수주잔고 열을 자가검증으로 찾는다 - 진행률 열을 합치면 안 된다."""

    def test_progress_percent_column_is_never_summed(self):
        snap = extract_order_backlog_snapshot([_doosan_table()], period="2025")
        self.assertIsNotNone(snap)
        # (705 + 12,967) 백만원 = 136.72억원. 진행률 합(1.99억 흉내)이 아니다.
        self.assertAlmostEqual(snap.point.value, 136.72, places=2)

    def test_multiple_segment_tables_are_summed(self):
        snap = extract_order_backlog_snapshot(
            [_doosan_table(), _doosan_table2()], period="2025")
        expected = (530600 - 529895 + 1004518 - 991551
                    + 4805196 - 2670 + 2341601 - 751881) / 100
        self.assertAlmostEqual(snap.point.value, expected, places=2)
        self.assertEqual(len(snap.tables), 2)

    def test_duplicate_tables_are_deduped(self):
        snap = extract_order_backlog_snapshot(
            [_doosan_table(), _doosan_table()], period="2025")
        self.assertAlmostEqual(snap.point.value, 136.72, places=2)
        self.assertEqual(len(snap.tables), 1)

    def test_table_provenance_is_preserved(self):
        """요구 1: 원단위·표 제목·원문 행 수·사용 행 수를 보존한다."""
        snap = extract_order_backlog_snapshot([_doosan_table()], period="2025")
        info = snap.tables[0]
        self.assertEqual(info["unit"], "백만원")
        self.assertEqual(info["source_rows"], 4)
        self.assertEqual(info["rows_used"], 2)
        self.assertIn("단위", info["caption"])
        self.assertEqual(info["raw_sum"], 13672.0)      # 백만원 원단위 합
        self.assertEqual(info["eok_sum"], 136.72)       # 변환 후 - 검산 쌍

    def test_total_row_cross_checks_detail_rows(self):
        """요구 4: 합계행과 세부행 합이 다르면 경고하고 합계행을 쓴다."""
        rows = _doosan_table().rows + [
            ["합계", "", "", "", "1,535,118", "1,521,446", "20,000", ""],
        ]
        snap = extract_order_backlog_snapshot(
            [DocumentTable(caption="(단위 : 백만원, %)", rows=rows)], period="2025")
        self.assertAlmostEqual(snap.point.value, 200.0, places=2)   # 합계행 우선
        self.assertTrue(any("합계" in w for w in snap.warnings), snap.warnings)

    def test_consistent_total_row_gives_no_warning(self):
        rows = _doosan_table().rows + [
            ["합계", "", "", "", "1,535,118", "1,521,446", "13,672", ""],
        ]
        snap = extract_order_backlog_snapshot(
            [DocumentTable(caption="(단위 : 백만원, %)", rows=rows)], period="2025")
        self.assertAlmostEqual(snap.point.value, 136.72, places=2)
        self.assertFalse(any("합계" in w for w in snap.warnings))

    def test_rotem_style_last_column_still_works(self):
        """수량/금액 쌍으로 열이 늘어나는 기존(현대로템형) 표는 그대로 맞아야 한다."""
        table = DocumentTable(
            caption="(단위 :백만원)",
            rows=[
                ["품목", "수주일자", "납기", "수주총액", "기납품액", "수주잔고"],
                ["수량", "금액", "수량", "금액", "수량", "금액"],
                ["철도A", "2024-01-01", "2028-12-31", "-", "100,000", "-", "10,000", "-", "90,000"],
                ["철도B", "2024-02-01", "2029-12-31", "-", "200,000", "-", "50,000", "-", "150,000"],
            ],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertAlmostEqual(snap.point.value, 2400.0, places=2)

    def test_identity_failure_refuses_to_guess(self):
        """항등식도 안 맞고 수주잔고가 마지막 열도 아니면 추측하지 않는다."""
        table = DocumentTable(
            caption="(단위 : 백만원)",
            rows=[
                ["품목", "수주총액", "기납품액", "수주잔고", "진행률"],
                ["A", "100,000", "90,000", "77,777", "55.0"],
                ["B", "200,000", "150,000", "88,888", "44.0"],
            ],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertIsNone(snap)


class ForeignCurrencyUnitTests(unittest.TestCase):
    """외화 표를 억원으로 가정하면 안 된다.

    실측(삼성바이오로직스): 수주 표가 '(단위: 백만 달러)'인데 예전에는 단위
    미인식으로 억원 라벨이 붙어 나갔다. 12,355 백만달러(약 18조원)가
    12,355억원으로 읽히는 라벨-값 계약 위반이다.
    """

    def _usd_table(self):
        return DocumentTable(
            caption="(단위: 백만 달러)",
            rows=[
                ["구분", "수주총액", "기납품액", "수주잔고"],
                ["CMO", "17,000", "6,296", "10,704"],
            ],
        )

    def test_usd_unit_is_recognized_not_assumed_eok(self):
        snap = extract_order_backlog_snapshot([self._usd_table()], period="2025")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.value_unit, "백만달러")
        self.assertEqual(snap.point.value, 10704.0)          # 환산하지 않는다
        self.assertEqual(snap.tables[0]["unit"], "백만달러")
        self.assertFalse(any("억원 가정" in w for w in snap.warnings))
        self.assertTrue(any("외화" in w or "달러" in w for w in snap.warnings),
                        snap.warnings)

    def test_krw_tables_win_over_foreign_when_both_exist(self):
        """같은 보고서에 원화·외화 표가 같이 있으면 원화를 쓰고 외화는 제외를 알린다."""
        snap = extract_order_backlog_snapshot(
            [self._usd_table(), _doosan_table()], period="2025")
        self.assertEqual(snap.value_unit, "억원")
        self.assertAlmostEqual(snap.point.value, 136.72, places=2)
        self.assertTrue(any("외화" in w for w in snap.warnings), snap.warnings)

    def test_million_won_is_not_confused_with_million_dollar(self):
        snap = extract_order_backlog_snapshot([_doosan_table()], period="2025")
        self.assertEqual(snap.value_unit, "억원")


class BacklogToolMetaTests(unittest.IsolatedAsyncioTestCase):
    """요구 3·5·6: meta v3, 중간 연도 누락 partial, anomaly."""

    @staticmethod
    def _meta(text):
        import json
        from dartlens import _result_meta as rmeta

        assert rmeta.MARKER_START in text, "메타 봉투가 없다"
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0]
        return json.loads(payload.strip())

    @staticmethod
    def _rcept(year):
        return f"{year + 1}0320{year}00"      # 14자리, 연도별 고유

    def _report(self, year, rcept=None):
        return {"rcept_no": rcept or self._rcept(year),
                "report_nm": f"사업보고서 ({year}.12)",
                "rcept_dt": f"{year + 1}0320", "corp_code": "00159616",
                "corp_name": "두산에너빌리티", "stock_code": "034020"}

    async def _run(self, tables_by_rcept, reports, years=3):
        async def fake_zip(rcept_no):
            return rcept_no.encode()

        def fake_tables(raw):
            return tables_by_rcept.get(raw.decode(), [])

        with patch.object(server, "_fetch_disclosure_list",
                          AsyncMock(return_value={"list": reports})), \
             patch.object(server, "_fetch_document_zip", AsyncMock(side_effect=fake_zip)), \
             patch.object(server, "extract_document_tables", side_effect=fake_tables):
            return await server.get_order_backlog(corp_code="00159616", years=years)

    async def test_missing_middle_years_are_partial_with_reason(self):
        reports = [self._report(y) for y in (2025, 2024, 2023, 2022, 2021, 2020, 2019)]
        tables = {self._rcept(2025): [_doosan_table()], self._rcept(2020): [_doosan_table()],
                  self._rcept(2019): [_doosan_table()]}
        text = await self._run(tables, reports)
        meta = self._meta(text)
        self.assertEqual(meta["data_completeness"], "partial")
        self.assertFalse(meta["coverage"]["coverage_complete"])
        missing = meta["coverage"]["missing_periods"]
        self.assertEqual(set(missing), {"2024", "2023", "2022", "2021"})
        body = text.split("RESULT_META_JSON_START")[0]
        self.assertIn("2024", body)   # 누락 사유가 본문에도 있다

    async def test_extraction_meta_carries_sources_and_units(self):
        reports = [self._report(y) for y in (2025, 2024, 2023)]
        tables = {self._rcept(y): [_doosan_table()] for y in (2025, 2024, 2023)}
        text = await self._run(tables, reports)
        meta = self._meta(text)
        ext = meta["backlog_extraction"]
        self.assertEqual(len(ext["source_filings"]), 3)
        self.assertEqual(ext["unit_normalization"]["target_unit"], "억원")
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_no_extractable_table_is_none_not_silent(self):
        reports = [self._report(2025, self._rcept(2025))]
        text = await self._run({self._rcept(2025): []}, reports)
        meta = self._meta(text)
        self.assertEqual(meta["data_completeness"], "none")

    async def test_anomalous_total_is_not_reported_as_backlog(self):
        """요구 5: 전체 잔고가 단일 세부 계약보다 작으면 자동 확정하지 않는다."""
        bad_total = DocumentTable(
            caption="수주상황 (단위 : 백만원)",
            rows=[
                ["품목", "발주처", "계약일", "공사기한", "수주총액", "기납품액", "수주잔고", "진행률"],
                ["체코 Dukovany", "한수원", "2025-12-15", "2038-04-18",
                 "4,805,196", "2,670", "4,802,526", "0.06"],
                ["합계", "", "", "", "4,805,196", "2,670", "26,158", ""],
            ],
        )
        reports = [self._report(2025, self._rcept(2025))]
        text = await self._run({self._rcept(2025): [bad_total]}, reports, years=1)
        meta = self._meta(text)
        body = text.split("RESULT_META_JSON_START")[0]
        # 261.58억원이 전체 잔고로 확정 표기되면 안 된다
        self.assertNotIn("2025=261.6", body)
        self.assertTrue(
            meta["data_completeness"] in ("none", "partial"), meta["data_completeness"])
        self.assertTrue(any("단일" in w or "작" in w for w in meta["warnings"]),
                        meta["warnings"])
