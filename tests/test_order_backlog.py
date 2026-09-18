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
    extract_order_backlog_snapshot,
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

    async def test_broken_total_cell_falls_back_to_verified_detail_sum(self):
        """합계행의 그 칸만 깨졌으면 검산을 통과한 세부합을 쓴다.

        같은 행의 수주총액·기납품액이 세부합과 정확히 맞는데 수주잔고만 세부합보다
        작으면, 합계가 틀린 게 아니라 그 칸 표기가 깨진 것이다(실측 삼성중공업
        2024 사업보고서: '315.350'). 어느 쪽이든 261.6억은 전체 잔고로 안 나간다.
        """
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
        self.assertNotIn("2025=261.6", body)
        self.assertIn("2025=48,025.3", body)          # 4,802,526 백만원
        self.assertTrue(any("깨진" in w for w in meta["warnings"]), meta["warnings"])

    async def test_unexplainable_small_total_is_still_refused(self):
        """요구 5: 설명이 안 되는 작은 합계는 여전히 자동 확정하지 않는다."""
        bad_total = DocumentTable(
            caption="수주상황 (단위 : 백만원)",
            rows=[
                ["품목", "발주처", "계약일", "공사기한", "수주총액", "기납품액", "수주잔고", "진행률"],
                ["체코 Dukovany", "한수원", "2025-12-15", "2038-04-18",
                 "4,805,196", "2,670", "4,802,526", "0.06"],
                ["신한울 3,4호기", "한수원", "2023-03-29", "2033-10-31",
                 "2,341,601", "751,881", "1,589,720", "32.11"],
                # 어느 칸도 세부합과 맞지 않는다 - 깨진 칸 하나로 설명되지 않는다
                ["합계", "", "", "", "1,000,000", "1,000", "26,158", ""],
            ],
        )
        reports = [self._report(2025, self._rcept(2025))]
        text = await self._run({self._rcept(2025): [bad_total]}, reports, years=1)
        meta = self._meta(text)
        body = text.split("RESULT_META_JSON_START")[0]
        self.assertNotIn("2025=261.6", body)
        self.assertTrue(
            meta["data_completeness"] in ("none", "partial"), meta["data_completeness"])
        self.assertTrue(any("단일" in w or "작" in w for w in meta["warnings"]),
                        meta["warnings"])


# ---------------------------------------------------------------------------
# 부문 열을 연도로 읽던 회귀 방지 (현대무벡스 2025 사업보고서 20260318001359)
#
# 실측: '기초 계약잔액 | 6,616,650 | 324,404,036 | 331,020,686' 한 행이
# '2025=66.2 | 2026=3,244.0 | 2027=3,310.2' 으로 나갔다. 숫자는 원문에 있는
# 진짜 값(IT사업부·물류사업부·합계)이고 틀린 건 이름표뿐이라, 검산으로도
# 안 걸리고 읽는 사람은 미래 수주 전망으로 오해한다.
#
# 원인 세 겹:
#   1) DART 원문의 껍데기 <TABLE> 안에 실제 표 224개가 들어 있는데 파서가
#      그걸 947행짜리 한 표로 뭉쳤다.
#   2) 그래서 머리글을 102행 위 주식선택권 '행사가능시점' 행에서 찾았다.
#   3) 롤포워드 표의 기초 행을 그 기간의 잔고로 내보냈다.
# ---------------------------------------------------------------------------

_MOVEX_STOCK_OPTION = """
        <TABLE>
          <TR><TD>행사가능시점</TD><TD>2025년 11월 03일</TD>
              <TD>2026년 09월 23일</TD><TD>2027년 03월 25일</TD></TR>
          <TR><TD>부여수량</TD><TD>550,166주</TD><TD>96,500주</TD><TD>229,928주</TD></TR>
        </TABLE>
"""


def _movex_balance_table(opening: tuple[str, str, str], ending: tuple[str, str, str]) -> str:
    return f"""
        <P>(단위: 천원)</P>
        <TABLE>
          <TR><TH>구분</TH><TH>IT사업부</TH><TH>물류사업부</TH><TH>합계</TH></TR>
          <TR><TD>기초 계약잔액</TD><TD>{opening[0]}</TD><TD>{opening[1]}</TD><TD>{opening[2]}</TD></TR>
          <TR><TD>증감액(*)</TD><TD>21,880,530</TD><TD>323,913,227</TD><TD>345,793,757</TD></TR>
          <TR><TD>수익 인식액</TD><TD>(23,685,708)</TD><TD>(331,788,916)</TD><TD>(355,474,624)</TD></TR>
          <TR><TD>기말 계약잔액</TD><TD>{ending[0]}</TD><TD>{ending[1]}</TD><TD>{ending[2]}</TD></TR>
        </TABLE>
"""


def _movex_xml(document_name: str, *, current: str, prior: str) -> bytes:
    """껍데기 <TABLE> 안에 주석 표들이 들어앉은 실제 DART 구조."""
    return f"""
    <DOCUMENT>
      <DOCUMENT-NAME>{document_name}</DOCUMENT-NAME>
      <SECTION-2>
        <TABLE BORDER="0" ACLASS="NORMAL">
          <TR><TD>
            <TITLE>주석</TITLE>
            {_MOVEX_STOCK_OPTION}
            <P>21. 주요 도급공사</P>
            {current}
            {prior}
          </TD></TR>
        </TABLE>
      </SECTION-2>
    </DOCUMENT>
    """.encode("utf-8")


# 별도(감사보고서) / 연결(연결감사보고서) 실측값 - 2025 사업보고서 기준
_MOVEX_SEPARATE = _movex_xml(
    "감사보고서",
    current=_movex_balance_table(
        ("6,616,650", "324,404,036", "331,020,686"),
        ("4,811,472", "316,528,347", "321,339,819")),
    prior=_movex_balance_table(
        ("5,983,455", "183,624,302", "189,607,757"),
        ("6,616,650", "324,404,036", "331,020,686")),
)
_MOVEX_CONSOLIDATED = _movex_xml(
    "연결감사보고서",
    current=_movex_balance_table(
        ("6,616,650", "372,543,997", "379,160,647"),
        ("4,811,472", "333,888,547", "338,700,019")),
    prior=_movex_balance_table(
        ("5,983,455", "193,124,273", "199,107,728"),
        ("6,616,650", "372,543,997", "379,160,647")),
)


class SegmentColumnsAreNotYearsTests(unittest.TestCase):
    def test_nested_tables_are_not_merged_into_one_table(self):
        tables = extract_document_tables(_MOVEX_SEPARATE)
        balance = [t for t in tables if any("기말 계약잔액" in "".join(r) for r in t.rows)]
        self.assertEqual(len(balance), 2)              # 당기·전기 두 벌
        for table in balance:
            self.assertEqual(len(table.rows), 5)       # 947행짜리 뭉텅이가 아니다
        self.assertTrue(
            all("행사가능시점" not in "".join("".join(r) for r in t.rows) for t in balance),
            "남의 표(주식선택권) 행이 섞여 들어왔다",
        )

    def test_segment_columns_are_never_labelled_as_years(self):
        tables = extract_document_tables(_MOVEX_SEPARATE)
        series = extract_order_backlog_series(tables, limit=4)
        self.assertIsNone(series, "부문 열을 기간 축으로 읽으면 안 된다")

    def test_ending_balance_of_current_period_is_reported(self):
        tables = extract_document_tables(_MOVEX_SEPARATE)
        snap = extract_order_backlog_snapshot(tables, period="2025")
        self.assertIsNotNone(snap)
        # 321,339,819천원 = 3,213.4억원. 기초(3,310.2억)도, 부문값(66.2억)도 아니다.
        self.assertAlmostEqual(snap.point.value, 3213.4, places=1)
        self.assertEqual(snap.tables[0]["row_label"], "기말 계약잔액")
        self.assertEqual(snap.value_unit, "억원")
        self.assertEqual(snap.tables[0]["unit"], "천원")

    def test_prior_period_table_is_not_mistaken_for_current(self):
        """전기 표의 기말은 당기 표의 기초와 같다 - 그걸로 당기를 가려낸다."""
        tables = extract_document_tables(_MOVEX_SEPARATE)
        snap = extract_order_backlog_snapshot(tables, period="2025")
        self.assertNotAlmostEqual(snap.point.value, 3310.2, places=1)

    def test_caption_comes_from_this_table_not_a_distant_one(self):
        tables = extract_document_tables(_MOVEX_SEPARATE)
        snap = extract_order_backlog_snapshot(tables, period="2025")
        self.assertIn("단위", snap.tables[0]["caption"])

    def test_consolidated_is_preferred_and_labelled(self):
        tables = (extract_document_tables(_MOVEX_SEPARATE)
                  + extract_document_tables(_MOVEX_CONSOLIDATED))
        snap = extract_order_backlog_snapshot(tables, period="2025")
        self.assertEqual(snap.basis, "연결")
        self.assertAlmostEqual(snap.point.value, 3387.0, places=1)

    def test_separate_only_report_still_works(self):
        snap = extract_order_backlog_snapshot(
            extract_document_tables(_MOVEX_SEPARATE), period="2025")
        self.assertEqual(snap.basis, "별도")


class PeriodAxisHeaderTests(unittest.TestCase):
    """머리글이 기간 축일 때만 연도를 붙인다."""

    def test_segment_header_yields_no_series(self):
        table = DocumentTable(
            caption="(단위: 천원)",
            rows=[
                ["구분", "IT사업부", "물류사업부", "합계"],
                ["기말 계약잔액", "4,811,472", "316,528,347", "321,339,819"],
            ],
        )
        self.assertIsNone(extract_order_backlog_series([table], limit=3))

    def test_distant_date_row_is_not_borrowed_as_header(self):
        """같은 표 안이어도 몇 행 위 날짜 행을 머리글로 끌어오지 않는다."""
        rows = [["행사가능시점", "2025년 11월 03일", "2026년 09월 23일", "2027년 03월 25일"]]
        rows += [[f"항목{i}", "1", "2", "3"] for i in range(8)]
        rows += [
            ["구분", "IT사업부", "물류사업부", "합계"],
            ["기말 계약잔액", "4,811,472", "316,528,347", "321,339,819"],
        ]
        self.assertIsNone(
            extract_order_backlog_series([DocumentTable(caption="", rows=rows)], limit=3))

    def test_year_header_still_works(self):
        table = DocumentTable(
            caption="(단위: 억원)",
            rows=[["구분", "2023", "2024", "2025"],
                  ["수주잔고", "1,000", "1,200", "1,500"]],
        )
        series = extract_order_backlog_series([table], limit=3)
        self.assertIsNotNone(series)
        self.assertEqual([p.period for p in series.points], ["2023", "2024", "2025"])

    def test_opening_balance_row_is_skipped_even_with_year_header(self):
        """연도 축이 맞아도 기초 잔액은 그 해의 잔고가 아니다."""
        table = DocumentTable(
            caption="(단위: 억원)",
            rows=[["구분", "2024", "2025"],
                  ["기초 수주잔고", "1,000", "1,200"],
                  ["기말 수주잔고", "1,200", "1,500"]],
        )
        series = extract_order_backlog_series([table], limit=3)
        self.assertEqual([p.value for p in series.points], [1200.0, 1500.0])

    def test_misaligned_header_is_refused(self):
        """머리글과 데이터 행의 칸 수가 다르면 밀어서 붙이지 않는다."""
        table = DocumentTable(
            caption="(단위: 억원)",
            rows=[["구분", "2023", "2024", "2025"],
                  ["수주잔고", "1,000", "1,200"]],
        )
        self.assertIsNone(extract_order_backlog_series([table], limit=3))


class RollforwardNoiseTests(unittest.TestCase):
    def test_allowance_rollforward_is_not_read_as_backlog(self):
        """손실충당금 롤포워드도 기초→기말 모양이라 '기말잔액'만 보면 통과했다."""
        table = DocumentTable(
            caption="보고기간종료일 현재 매출채권에 대한 손실충당금은 다음과 같습니다.",
            rows=[
                ["(단위: 천원)"],
                ["구분", "기초잔액", "설정액", "기말잔액"],
                ["매출채권", "212,324", "4,334,143", "4,764,458"],
            ],
        )
        self.assertIsNone(extract_order_backlog_point([table], period="2025"))

    def test_deferred_tax_rollforward_is_not_read_as_backlog(self):
        table = DocumentTable(
            caption="이연법인세자산과 부채의 변동",
            rows=[
                ["(단위: 천원)"],
                ["구분", "기초잔액", "손익계산서", "기말잔액"],
                ["합계", "710,777", "1,793,871", "3,433,634"],
            ],
        )
        self.assertIsNone(extract_order_backlog_point([table], period="2025"))


class SeriesConsistencyTests(unittest.IsolatedAsyncioTestCase):
    """한 시계열에 서로 다른 기준·방식·순서를 섞지 않는다."""

    @staticmethod
    def _zip(xml: bytes) -> bytes:
        out = BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("report.xml", xml)
        return out.getvalue()

    async def _run(self, docs, reports, years=3):
        fetch_doc = AsyncMock(side_effect=lambda rcept_no: docs[rcept_no])
        with (
            patch("dartlens._safe.is_licensed", return_value=True),
            patch.object(server, "_fetch_disclosure_list",
                         AsyncMock(return_value={"list": reports})),
            patch.object(server, "_fetch_document_zip", fetch_doc),
        ):
            text = await server.get_order_backlog("01358463", years=years)
        return text.split("RESULT_META_JSON_START")[0]

    async def test_consolidated_and_separate_are_not_mixed(self):
        """실측: 2024는 연결 3,791.6억, 2025는 별도 3,213.4억이 한 줄에 섞였다."""
        docs = {
            "20260318001359": self._zip(_MOVEX_SEPARATE),      # 별도만 실린 해
            "20250318001331": self._zip(_MOVEX_CONSOLIDATED),  # 연결만 실린 해
        }
        reports = [
            {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260318001359",
             "rcept_dt": "20260318"},
            {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20250318001331",
             "rcept_dt": "20250318"},
        ]
        body = await self._run(docs, reports)
        self.assertIn("재무기준: 별도재무제표", body)
        self.assertNotIn("3,791.6", body)
        self.assertIn("재무기준 상이", body)

    async def test_half_year_point_sorts_before_year_end(self):
        """'2025'는 2025년 12월말이다. 문자열 정렬이면 2025.06 뒤로 간다."""
        docs = {
            "20260318001359": self._zip(_MOVEX_SEPARATE),
            "20250813001630": self._zip(_MOVEX_SEPARATE),
        }
        reports = [
            {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260318001359",
             "rcept_dt": "20260318"},
            {"report_nm": "반기보고서 (2025.06)", "rcept_no": "20250813001630",
             "rcept_dt": "20250813"},
        ]
        body = await self._run(docs, reports)
        self.assertLess(body.index("2025.06="), body.index("| 2025="),
                        "6월말 잔고가 연말 뒤에 놓였다")


# ---------------------------------------------------------------------------
# 괄호 음수 때문에 검산이 아예 안 돌던 회귀 (한화오션 2025 사업보고서 20260317000644)
#
# 실측: 기납품액이 '(10,322,942)' 괄호 음수라 숫자로 안 읽혔다. 그러면 행마다
# 숫자가 두 개뿐이라 항등식(수주총액=기납품액+수주잔고)을 세울 수 없고, 검산이
# 안 돌았다는 표시조차 안 남는다. 그 표는 단일 값 경로로 새어, 부문 합계
# (34.5조) 대신 첫 행 '상선'(26.0조)이 수주잔고로 나갔다.
# ---------------------------------------------------------------------------


def _hanwha_ocean_table():
    return DocumentTable(
        caption="(단위 : 백만원)",
        rows=[
            ["품목", "수주일자", "납기", "수주총액(*1)", "기납품액(*2)", "수주잔고"],
            ["수량", "금액", "수량", "금액", "수량", "금액"],
            ["상선", "2025.12.31일까지", "-", "-", "36,326,613", "-",
             "(10,322,942)", "-", "26,003,671"],
            ["해양 및 특수선", "2025.12.31일까지", "-", "-", "8,186,289", "-",
             "(1,884,332)", "-", "6,301,957"],
            ["플랜트", "2025.12.31일까지", "-", "-", "3,002,968", "-",
             "(816,095)", "-", "2,186,873"],
            ["기타", "2025.12.31일까지", "-", "-", "6,255", "-", "(3,692)", "-", "2,563"],
            ["합 계", "-", "47,522,125", "-", "(13,027,061)", "-", "34,495,064"],
        ],
    )


class ParenthesisedNegativeTests(unittest.TestCase):
    def test_parenthesised_number_is_negative(self):
        from dartlens._order_backlog import _plain_number

        self.assertEqual(_plain_number("(10,322,942)"), -10322942.0)
        self.assertEqual(_plain_number("(3,692)"), -3692.0)

    def test_footnote_and_unit_markers_are_still_not_numbers(self):
        from dartlens._order_backlog import _plain_number

        for cell in ("(*1)", "(단위: 천원)", "(주1)", "()", "-"):
            self.assertIsNone(_plain_number(cell), cell)

    def test_segment_total_is_summed_not_the_first_row(self):
        snap = extract_order_backlog_snapshot([_hanwha_ocean_table()], period="2025")
        self.assertIsNotNone(snap)
        # 34,495,064 백만원 = 344,950.64억. '상선' 한 줄(260,036.71억)이 아니다.
        self.assertAlmostEqual(snap.point.value, 344950.64, places=2)
        self.assertEqual(snap.tables[0]["rows_used"], 4)
        self.assertIn("항등식", snap.tables[0]["method"])

    def test_positive_delivered_column_still_works(self):
        """기납품액을 양수로 적는 표(두산형)는 그대로 맞아야 한다."""
        snap = extract_order_backlog_snapshot([_doosan_table()], period="2025")
        self.assertAlmostEqual(snap.point.value, 136.72, places=2)


# ---------------------------------------------------------------------------
# 단위 표기 없는 표를 섞어 합계를 만들던 회귀 (현대건설 [기재정정]사업보고서
# 20251017000151 / 반기보고서 20250814002545)
#
# 실측: '(2) 현대엔지니어링' 표만 단위 표기가 없어 백만원을 억원으로 읽었고,
# 191,004억이 19,100,399억이 돼 2024년 수주잔고가 1,938조로 나갔다. 반기보고서는
# 두 표 다 표기가 없어 4,337조가 됐다. 원문에 그 표들의 단위는 실제로 없다.
# ---------------------------------------------------------------------------


def _hdec_table(caption, scale=1):
    """현대건설형 공사별 상세표. 합계행 없이 개별 공사만 있는 축약본."""
    def amount(value):
        return f"{value * scale:,}"

    return DocumentTable(
        caption=caption,
        rows=[
            ["구분", "공사명", "발주처", "수주총액", "기납품액", "계약잔액"],
            ["국내", "A현장", "발주처1", amount(3931885), amount(312881), amount(3619004)],
            ["해외", "B현장", "발주처2", amount(2411552), amount(903131), amount(1508421)],
        ],
    )


class UnknownUnitSumTests(unittest.TestCase):
    def test_unit_less_table_is_not_summed_with_declared_ones(self):
        snap = extract_order_backlog_snapshot(
            [_hdec_table("(단위 : 백만원)"), _hdec_table("(2) 현대엔지니어링", scale=2)],
            period="2024",
        )
        self.assertTrue(snap.unit_unknown)
        self.assertTrue(any("단위 표기가 없어" in w for w in snap.warnings), snap.warnings)

    def test_all_tables_without_unit_are_also_refused(self):
        snap = extract_order_backlog_snapshot(
            [_hdec_table("다. 수주상황 1) 현대건설"), _hdec_table("2) 현대엔지니어링", scale=2)],
            period="2025.06",
        )
        self.assertTrue(snap.unit_unknown)

    def test_declared_units_are_unaffected(self):
        snap = extract_order_backlog_snapshot(
            [_hdec_table("(단위 : 백만원)"), _hdec_table("(단위 : 백만원)", scale=2)],
            period="2024",
        )
        self.assertFalse(snap.unit_unknown)
        self.assertAlmostEqual(snap.point.value, (3619004 + 1508421) * 3 / 100, places=2)


class UnknownUnitToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_period_with_unknown_unit_is_reported_missing_not_guessed(self):
        good = [_hdec_table("(단위 : 백만원)")]
        bad = [_hdec_table("(단위 : 백만원)"), _hdec_table("(2) 현대엔지니어링", scale=2)]
        tables = {"good": good, "bad": bad}

        async def fake_zip(rcept_no):
            return (b"bad" if rcept_no.startswith("2025") else b"good")

        reports = [
            {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260318001395",
             "rcept_dt": "20260318"},
            {"report_nm": "사업보고서 (2024.12)", "rcept_no": "20250318001395",
             "rcept_dt": "20250318"},
        ]
        with (
            patch("dartlens._safe.is_licensed", return_value=True),
            patch.object(server, "_fetch_disclosure_list",
                         AsyncMock(return_value={"list": reports})),
            patch.object(server, "_fetch_document_zip", AsyncMock(side_effect=fake_zip)),
            patch.object(server, "extract_document_tables",
                         side_effect=lambda raw: tables[raw.decode()]),
        ):
            text = await server.get_order_backlog("00164478", years=3)
        body = text.split("RESULT_META_JSON_START")[0]
        self.assertIn("단위 표기 없는 표가 있어", body)
        self.assertNotIn("2024=", body)          # 100배 값이 시계열에 안 들어간다
        self.assertIn("2025=", body)


# ---------------------------------------------------------------------------
# 병합 셀 때문에 원문이 적어 놓은 합계를 못 읽던 회귀
# (현대건설 2025 사업보고서 20260318001395 / 한화에어로 20260316001112 /
#  삼성바이오로직스 20260515001658)
#
# 실측: 수주상황 표는 개별 공사 23건 + '기타' 한 줄 + 소계 + '국내 / 해외 합계'로
# 끝난다. 합계행은 COLSPAN 으로 라벨이 5칸을 덮어 4칸, 개별 공사 행은 구분 열이
# ROWSPAN 이라 7칸, 헤더는 8칸이었다. 열 번호가 행마다 달라 항등식이 찾아낸 열이
# 합계행엔 아예 없었고, 40.6조짜리 '기타' 줄과 69.7조 합계가 통째로 빠져
# 개별 공사만 더한 22.7조가 나갔다.
# ---------------------------------------------------------------------------

MERGED_ORDER_TABLE = """
<DOCUMENT>
  <TABLE>
    <TR><TD>(단위 : 백만원)</TD></TR>
  </TABLE>
  <TABLE>
    <TR><TH>구분</TH><TH>공사명</TH><TH>발주처</TH><TH>수주총액</TH>
        <TH>완성공사액</TH><TH>계약잔액</TH></TR>
    <TR><TD ROWSPAN="3">국내</TD><TD>A현장</TD><TD>발주처1</TD>
        <TD>3,931,885</TD><TD>988,332</TD><TD>2,943,553</TD></TR>
    <TR><TD>B현장</TD><TD>발주처2</TD><TD>2,438,912</TD><TD>2,012,262</TD><TD>426,650</TD></TR>
    <TR><TD COLSPAN="2">기타</TD><TD>57,723,031</TD><TD>18,761,243</TD><TD>38,961,788</TD></TR>
    <TR><TD COLSPAN="3">국내합계</TD><TD>64,093,828</TD><TD>21,761,837</TD><TD>42,331,991</TD></TR>
  </TABLE>
</DOCUMENT>
""".encode("utf-8")


class MergedCellAlignmentTests(unittest.TestCase):
    def test_every_row_has_the_same_column_count(self):
        table = [t for t in extract_document_tables(MERGED_ORDER_TABLE)
                 if len(t.rows) > 1][0]
        self.assertEqual({len(r) for r in table.rows}, {6})
        self.assertEqual(table.rows[2][0], "국내")      # ROWSPAN 이 아래 행까지
        self.assertEqual(table.rows[3][:3], ["국내", "기타", ""])   # COLSPAN 은 빈 칸으로

    def test_total_row_wins_over_listed_projects(self):
        tables = extract_document_tables(MERGED_ORDER_TABLE)
        snap = extract_order_backlog_snapshot(tables, period="2025")
        self.assertIsNotNone(snap)
        # 원문 합계 42,331,991 백만원 = 423,319.91억. 개별 공사만 더한
        # (2,943,553 + 426,650) = 33,702.03억이 아니다.
        self.assertAlmostEqual(snap.point.value, 423319.91, places=2)
        self.assertIn("원문 합계행", snap.tables[0]["method"])
        self.assertEqual(snap.tables[0]["rows_used"], 1)


class GrandTotalChoiceTests(unittest.TestCase):
    def test_subtotals_and_grand_total_pick_the_grand_total(self):
        from dartlens._order_backlog import _grand_total

        self.assertEqual(_grand_total([55398875.0, 14336700.0, 69735574.0]), 69735574.0)

    def test_single_total_is_used(self):
        from dartlens._order_backlog import _grand_total

        self.assertEqual(_grand_total([34495064.0]), 34495064.0)

    def test_unrelated_totals_are_refused(self):
        from dartlens._order_backlog import _grand_total

        self.assertIsNone(_grand_total([100.0, 500.0, 900.0]))


class DuplicateSummaryTableTests(unittest.TestCase):
    """같은 수주잔고를 요약표와 상세표로 두 번 싣는 보고서(한화에어로)."""

    @staticmethod
    def _table(caption, rows):
        return DocumentTable(caption=caption, rows=[
            ["부문", "품목", "수주총액", "기납품액", "수주잔고"], *rows])

    def test_same_total_counted_once(self):
        summary = self._table("(단위 : 백만원) 부문별 요약", [
            ["항공", "상세내역 참조", "44,481,780", "12,082,235", "32,399,545"],
            ["방산", "상세내역 참조", "52,558,369", "15,338,467", "37,219,902"],
        ])
        detail = self._table("(단위 : 백만원) 계약별 상세", [
            ["항공", "추진기관", "44,481,780", "12,082,235", "32,399,545"],
            ["방산", "유도무기", "52,558,369", "15,338,467", "37,219,902"],
        ])
        snap = extract_order_backlog_snapshot([summary, detail], period="2025")
        self.assertAlmostEqual(snap.point.value, (32399545 + 37219902) / 100, places=2)
        self.assertEqual(len(snap.tables), 1)
        self.assertTrue(any("두 번 실은" in w for w in snap.warnings), snap.warnings)

    def test_different_totals_are_still_summed(self):
        first = self._table("(단위 : 백만원)", [
            ["조선", "상선", "10,000", "4,000", "6,000"]])
        second = self._table("(단위 : 백만원)", [
            ["해양", "플랜트", "20,000", "5,000", "15,000"]])
        snap = extract_order_backlog_snapshot([first, second], period="2025")
        self.assertAlmostEqual(snap.point.value, 210.0, places=2)
        self.assertEqual(len(snap.tables), 2)


class ScenarioRowTests(unittest.TestCase):
    """같은 계약을 '○○ 기준'으로 두 번 적은 줄은 더하지 않는다(삼성바이오로직스)."""

    def _table(self):
        return DocumentTable(
            caption="(단위: 백만 달러)",
            rows=[
                ["사업부문", "품목", "수주일자", "납기", "구분", "", "수주총액", "기납품액", "수주잔고"],
                ["CDMO", "항체의약품", "2015년~(계약별상이)", "~2037년(계약별상이)",
                 "현 최소구매물량 기준", "금액", "21,153", "10,449", "10,704"],
                ["CDMO", "항체의약품", "2015년~(계약별상이)", "~2037년(계약별상이)",
                 "고객사 제품개발 성공시예상 수요물량 기준", "금액", "23,881", "10,449", "13,432"],
            ],
        )

    def test_scenario_rows_are_not_added(self):
        snap = extract_order_backlog_snapshot([self._table()], period="2025")
        self.assertEqual(snap.point.value, 10704.0)     # 24,136 이 아니다
        self.assertEqual(snap.value_unit, "백만달러")
        self.assertTrue(any("다른 기준" in w for w in snap.warnings), snap.warnings)

    def test_real_segment_rows_are_still_added(self):
        table = DocumentTable(
            caption="(단위 : 백만원)",
            rows=[
                ["부문", "품목", "수주총액", "기납품액", "수주잔고"],
                ["조선", "상선", "10,000", "4,000", "6,000"],
                ["조선", "특수선", "20,000", "5,000", "15,000"],
            ],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertAlmostEqual(snap.point.value, 210.0, places=2)


# ---------------------------------------------------------------------------
# 합계 열이 없는 표에서 맨 오른쪽 칸을 전체 값으로 읽던 회귀
# (현대엘리베이터 2025 사업보고서 20260318001372)
#
# 실측: 연결 계약잔액 표는 합계 열 없이 부문 4개만 있다. 맨 오른쪽 IT사업부문
# 914,773천원(9.1억)이 회사 전체 수주잔고로 나갔다(실제 합 20,908억). 같은
# 보고서의 별도 표는 '당기|전기' 구성이라 맨 오른쪽이 전기 - 한 해 묵은 값이다.
# ---------------------------------------------------------------------------


class ValueColumnChoiceTests(unittest.TestCase):
    CONSOLIDATED = DocumentTable(
        caption="(단위: 천원)",
        rows=[
            ["구 분", "물품취급장비부문", "건설업부문", "물류사업부문", "IT사업부문"],
            ["기초 계약잔액", "1,350,400,047", "144,805,080", "372,394,320", "614,552"],
            ["증  감  액 (*)", "1,172,013,366", "418,458,121", "331,749,160", "3,131,793"],
            ["수 익 인 식 액", "(1,221,151,952)", "(108,513,404)", "(370,254,932)", "(2,831,572)"],
            ["기말 계약잔액", "1,301,261,461", "454,749,797", "333,888,548", "914,773"],
        ],
    )
    SEPARATE = DocumentTable(
        caption="(단위: 천원)",
        rows=[
            ["구 분", "당기", "전기"],
            ["기초 계약잔액", "1,239,323,462", "1,378,426,538"],
            ["기말 계약잔액", "1,200,865,507", "1,239,323,462"],
        ],
    )

    def test_segment_columns_without_total_are_summed(self):
        point = extract_order_backlog_point([self.CONSOLIDATED], period="2025")
        self.assertIsNotNone(point)
        # 1,301,261,461+454,749,797+333,888,548+914,773 = 2,090,814,579천원
        self.assertAlmostEqual(point.value, 20908.15, places=2)

    def test_last_segment_is_never_the_whole_backlog(self):
        point = extract_order_backlog_point([self.CONSOLIDATED], period="2025")
        self.assertNotAlmostEqual(point.value, 9.14773, places=2)   # IT사업부문만

    def test_period_columns_pick_current_not_previous(self):
        point = extract_order_backlog_point([self.SEPARATE], period="2025")
        self.assertAlmostEqual(point.value, 12008.66, places=2)     # 당기
        self.assertNotAlmostEqual(point.value, 12393.23, places=2)  # 전기

    def test_total_column_still_wins(self):
        table = DocumentTable(
            caption="(단위:백만원)",
            rows=[
                ["구분", "조선", "해양플랜트", "기타", "합계"],
                ["기말계약잔액", "44,350,193", "2,445,087", "9,586,022", "56,381,302"],
            ],
        )
        point = extract_order_backlog_point([table], period="2025")
        self.assertAlmostEqual(point.value, 563813.02, places=2)

    def test_short_total_label_is_recognised_as_total_column(self):
        table = DocumentTable(
            caption="(단위:백만원)",
            rows=[
                ["구분", "조선", "해양", "계"],
                ["기말계약잔액", "10,000", "20,000", "30,000"],
            ],
        )
        point = extract_order_backlog_point([table], period="2025")
        self.assertAlmostEqual(point.value, 300.0, places=2)   # 60,000 이 아니다

    def test_data_row_is_not_mistaken_for_header(self):
        """'기초계약잔액 | 33,937,341 | ...' 은 낱말이 맞아도 머리글이 아니다."""
        from dartlens._order_backlog import _nearest_header

        rows = [
            ["(단위:백만원)"],
            ["구분", "조선", "기타", "합계"],
            ["기초계약잔액", "33,937,341", "9,355,847", "46,922,936"],
            ["기말계약잔액", "44,350,193", "9,586,022", "56,381,302"],
        ]
        self.assertEqual(_nearest_header(rows, before=3), rows[1])


# ---------------------------------------------------------------------------
# 합계행을 못 알아보거나, 겹치는 표를 더하거나, 표를 아예 못 읽던 회귀
# (금호건설 20240318000777 / 두산에너빌리티 20260320001246 /
#  계룡건설 20240319000660 / 태영건설 20240927000935 / 한국항공우주 20260318001461)
# ---------------------------------------------------------------------------


class TotalRowLabelTests(unittest.TestCase):
    """회사마다 합계행을 다르게 적는다. 놓치면 소계까지 더해 몇 배가 된다."""

    def test_reference_suffix_is_ignored(self):
        from dartlens._order_backlog import _is_total_row

        for label in ("총계(E=C+D)", "해외합계(D)", "국내합계(C=A+B)", "해외토목 계(D)"):
            self.assertTrue(_is_total_row([label]), label)

    def test_total_word_at_the_front(self):
        from dartlens._order_backlog import _is_total_row

        self.assertTrue(_is_total_row(["합계 - 전체"]))
        self.assertTrue(_is_total_row(["소계 (국내)"]))

    def test_spaced_and_nested_labels(self):
        from dartlens._order_backlog import _is_total_row

        for label in ("합 계", "국내 / 해외 합계", "계", "국내합계"):
            self.assertTrue(_is_total_row([label]), label)

    def test_ordinary_labels_are_not_totals(self):
        from dartlens._order_backlog import _is_total_row

        for label in ("기타", "설계", "토목설계", "국내 토목", "회계법인",
                      "호남고속철도2단계(고막원~목포) 제2공구 건설공사"):
            self.assertFalse(_is_total_row([label]), label)


class NestedSubtotalTests(unittest.TestCase):
    def test_grand_total_among_nested_subtotals(self):
        from dartlens._order_backlog import _grand_total

        # 금호건설 실측: 국내토목 계·국내건축 합계·국내합계·해외토목 계·해외합계·총계
        values = [2375225.0, 4617143.0, 6992368.0, 100177.0, 100177.0, 7092545.0]
        self.assertEqual(_grand_total(values), 7092545.0)

    def test_unrelated_totals_are_still_refused(self):
        from dartlens._order_backlog import _grand_total

        self.assertIsNone(_grand_total([100.0, 500.0, 900.0]))


class OverlappingTableTests(unittest.TestCase):
    """같은 공사가 두 표에 실리면 더하지 않는다."""

    @staticmethod
    def _table(caption, projects):
        rows = [["구분", "공사명", "발주처", "수주총액", "완성공사액", "계약잔액"]]
        for name, total, done, left in projects:
            rows.append(["국내", name, "발주처", f"{total:,}", f"{done:,}", f"{left:,}"])
        return DocumentTable(caption=caption, rows=rows)

    FULL = [("A현장", 100000, 40000, 60000), ("B현장", 80000, 30000, 50000),
            ("C현장", 60000, 20000, 40000), ("D현장", 40000, 10000, 30000)]

    def test_excerpt_table_is_not_added_to_the_full_one(self):
        full = self._table("(단위 : 백만원) 전체 공사", self.FULL)
        excerpt = self._table("(단위 : 백만원) 주요 공사", self.FULL[:3])
        snap = extract_order_backlog_snapshot([full, excerpt], period="2025")
        self.assertAlmostEqual(snap.point.value, 1800.0, places=2)   # 60+50+40+30 백만원
        self.assertEqual(len(snap.tables), 1)
        self.assertTrue(any("발췌" in w for w in snap.warnings), snap.warnings)

    def test_different_projects_are_still_summed(self):
        first = self._table("(단위 : 백만원)", self.FULL[:2])
        second = self._table("(단위 : 백만원)", [("E현장", 50000, 20000, 30000),
                                                 ("F현장", 30000, 10000, 20000),
                                                 ("G현장", 20000, 5000, 15000)])
        snap = extract_order_backlog_snapshot([first, second], period="2025")
        self.assertAlmostEqual(snap.point.value, 1750.0, places=2)
        self.assertEqual(len(snap.tables), 2)

    def test_two_row_tables_are_not_merged_by_label(self):
        """'관계사/비관계사'처럼 이름이 같은 두어 줄짜리 표는 겹침 근거가 못 된다."""
        def tiny(left):
            return DocumentTable(caption="(단위 : 백만원)", rows=[
                ["구분", "수주총액", "완성공사액", "계약잔액"],
                ["관계사", f"{left * 3:,}", f"{left * 2:,}", f"{left:,}"],
                ["비관계사", "-", "-", "-"],
            ])
        snap = extract_order_backlog_snapshot([tiny(9262), tiny(5998)], period="2025")
        self.assertEqual(len(snap.tables), 2)
        self.assertAlmostEqual(snap.point.value, (9262 + 5998) / 100, places=2)


class DetailHeaderWordTests(unittest.TestCase):
    def test_site_name_header_is_recognised_as_detail_table(self):
        """계룡건설 표는 첫 열이 '현장명'이라 상세표로 안 잡혔다."""
        table = DocumentTable(
            caption="(단위 : 백만원 )",
            rows=[
                ["현장명", "착공일", "완공예정일", "총 도급금액", "완성공사액", "계약잔액"],
                ["인천검단 택지개발 조경1-2", "2019-12-02", "2024-06-30", "26,562", "24,958", "1,604"],
                ["충북대병원 의생명진료연구동", "2020-01-15", "2024-09-17", "25,400", "23,531", "1,869"],
                ["기    타", "", "", "6,513,225", "1,101,647", "5,411,578"],
                ["합    계", "", "", "14,518,383", "5,120,396", "9,397,987"],
            ],
        )
        snap = extract_order_backlog_snapshot([table], period="2025")
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.point.value, 93979.87, places=2)
        self.assertIn("원문 합계행", snap.tables[0]["method"])


class TransposedRollforwardTests(unittest.TestCase):
    """행이 당기·전기, 열이 기초→이월인 표(한국항공우주)."""

    TABLE = DocumentTable(
        caption="가. 당기와 전기 중 공사계약 잔액의 변동내역은 다음과 같습니다.",
        rows=[
            ["(단위: 천원)", "", "", "", "", ""],
            ["구분", "기초계약잔액", "증감액(*1)", "사업결합으로인한 증감", "수익인식", "이월계약잔액(*2)"],
            ["당기", "6,797,434,572", "1,184,026,555", "37,070,697", "(1,739,692,038)", "6,278,839,786"],
            ["전기", "8,122,814,460", "402,842,589", "-", "(1,728,222,477)", "6,797,434,572"],
        ],
    )

    def test_current_period_ending_balance_is_read(self):
        point = extract_order_backlog_point([self.TABLE], period="2025")
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.value, 62788.39786, places=3)

    def test_rollforward_stages_are_never_summed(self):
        point = extract_order_backlog_point([self.TABLE], period="2025")
        stages = (6797434572 + 1184026555 + 37070697 - 1739692038 + 6278839786) / 100000
        self.assertNotAlmostEqual(point.value, stages, places=2)

    def test_prior_period_row_is_not_used(self):
        point = extract_order_backlog_point([self.TABLE], period="2025")
        self.assertNotAlmostEqual(point.value, 67974.34572, places=3)


class ForeignUnitSpellingTests(unittest.TestCase):
    """'천USD' 를 '달러'로 읽으면 1,000배 어긋난다(일진전기 20240313001767)."""

    def test_thousand_usd_is_not_plain_dollar(self):
        from dartlens._order_backlog import _table_unit

        table = DocumentTable(caption="(단위 : 천USD )", rows=[["구분", "수주잔고"]])
        self.assertEqual(_table_unit(table), "천달러")

    def test_million_usd_spellings(self):
        from dartlens._order_backlog import _table_unit

        for caption in ("(단위: 백만USD)", "(단위: 백만US$)", "(단위 : 백만불)"):
            table = DocumentTable(caption=caption, rows=[["구분", "수주잔고"]])
            self.assertEqual(_table_unit(table), "백만달러", caption)

    def test_plain_dollar_still_works(self):
        from dartlens._order_backlog import _table_unit

        table = DocumentTable(caption="(단위 : USD)", rows=[["구분", "수주잔고"]])
        self.assertEqual(_table_unit(table), "달러")
