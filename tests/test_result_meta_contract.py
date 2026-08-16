"""결과 메타 봉투 규약 v1 — DartLens 쪽 계약 테스트.

`_result_meta.py`는 세 Lens에 **같은 내용으로 복사**되는 파일이다. 여기서는
(1) 복사본이 규약 버전을 지키는지, (2) DartLens가 기준일을 근거 공시의 접수일로
채우는지, (3) StockLens로 넘길 6자리 stock_code를 흘려주는지를 본다.

Lens 간 이어달리기의 실제 경로:
    search_company("디오") → entity.corp_code(8) + entity.stock_code(6)
    get_major_accounts(corp_code) → data_as_of = 반기보고서 접수일
    → StockLens get_event_reaction(code=stock_code, event_date=data_as_of)
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import _result_meta as rmeta
from dartlens.server import _dart_meta, _identity_meta


class ContractVersionTests(unittest.TestCase):
    def test_meta_version_matches_other_lenses(self):
        """세 Lens가 같은 규약을 쓰는지 확인하는 유일한 표식. 올릴 땐 셋 다 함께."""
        self.assertEqual(rmeta.META_VERSION, 2)

    def test_marker_matches_stocklens(self):
        """StockLens가 먼저 쓰던 마커를 그대로 승계한다(파서 호환)."""
        self.assertEqual(rmeta.MARKER_START, "RESULT_META_JSON_START")
        self.assertEqual(rmeta.MARKER_END, "RESULT_META_JSON_END")

    def test_day_normalization_handles_dart_format(self):
        """DART는 20260813, 네이버는 2026.08.14로 준다."""
        self.assertEqual(rmeta.normalize_day("20260813"), "2026-08-13")
        self.assertEqual(rmeta.normalize_day("2026/08/13(연결)"), "2026-08-13")
        self.assertIsNone(rmeta.normalize_day("미상"))


class DartMetaTests(unittest.TestCase):
    # fnlttSinglAcnt 실제 응답 발췌 — corp_code와 stock_code가 함께 온다.
    ROWS = [
        {
            "rcept_no": "20260813001067",
            "corp_code": "00115931",
            "stock_code": "039840",
            "account_nm": "매출액",
        }
    ]

    def test_data_as_of_is_the_filing_receipt_date(self):
        """rcept_no 앞 8자리가 접수일. 별도 필드 없이도 기준일이 나온다."""
        m = _dart_meta(rows=self.ROWS, corp_code="00115931")
        self.assertEqual(m["data_as_of"], "2026-08-13")
        self.assertEqual(m["data_basis"], "filing")

    def test_entity_carries_both_identifiers(self):
        """DartLens만 두 코드를 다 안다. 흘려보내야 다음 Lens가 재검색을 안 한다."""
        m = _dart_meta(rows=self.ROWS, corp_code="00115931")
        self.assertEqual(m["entity"]["corp_code"], "00115931")
        self.assertEqual(m["entity"]["stock_code"], "039840")

    def test_explicit_receipt_date_wins_over_rcept_no(self):
        m = _dart_meta(rows=self.ROWS, rcept_dt="20260701")
        self.assertEqual(m["data_as_of"], "2026-07-01")

    def test_period_is_a_label_not_a_fabricated_date(self):
        """재무는 기간이 기준이다. '2026 반기'를 날짜로 만들지 않는다."""
        m = _dart_meta(rows=self.ROWS, data_period="2026 반기보고서")
        self.assertEqual(m["data_period"], "2026 반기보고서")

    def test_empty_rows_yield_no_fake_date(self):
        m = _dart_meta(rows=[], corp_code="00115931", data_completeness=rmeta.NONE)
        self.assertIsNone(m["data_as_of"])
        self.assertEqual(m["data_completeness"], "none")

    def test_lens_is_tagged(self):
        """출처가 섞이면 사용자가 어느 Lens 숫자인지 못 가린다."""
        self.assertEqual(_dart_meta(rows=self.ROWS)["lens"], "dartlens")


class IdentityMetaTests(unittest.TestCase):
    class _Entry:
        corp_code = "00115931"
        stock_code = "039840"
        corp_name = "디오"

    def test_search_company_exposes_both_codes(self):
        m = _identity_meta(self._Entry())
        self.assertEqual(
            m["entity"],
            {"stock_code": "039840", "corp_code": "00115931", "name": "디오"},
        )

    def test_identifier_mapping_has_no_reference_date(self):
        """코드 대응은 시점 데이터가 아니다. 날짜를 지어내면 안 된다."""
        self.assertIsNone(_identity_meta(self._Entry())["data_as_of"])


class HandoffChainTests(unittest.TestCase):
    def test_receipt_date_is_directly_usable_as_event_date(self):
        """StockLens get_event_reaction(event_date=...)이 받는 형태 그대로여야 한다."""
        m = _dart_meta(rows=DartMetaTests.ROWS)
        self.assertRegex(m["data_as_of"], r"^\d{4}-\d{2}-\d{2}$")

    def test_as_of_and_data_as_of_do_not_collide(self):
        m = rmeta.build_meta(
            lens="dartlens", data_basis=rmeta.BASIS_FILING, data_as_of="20260813",
            now=datetime(2026, 8, 16, 10, 0, tzinfo=rmeta.KST),
        )
        self.assertTrue(m["as_of"].startswith("2026-08-16"))
        self.assertEqual(m["data_as_of"], "2026-08-13")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CorrectionFilingTests(unittest.TestCase):
    """정정공시 — "이 숫자는 그 뒤 정정됐습니다"를 말할 수 있는가.

    DART API는 정정 반영본을 주므로 숫자 자체는 최신이 맞다. 문제는 그게 정정된
    값이라는 사실을 말할 방법이 없었다는 것. 최근 5일 공시 100건 중 20건이
    정정이었고, 부방은 2024 사업보고서를 2026-08-14에 정정했다(1년 8개월 뒤).
    그 사이 같은 조회를 한 사람은 지금과 다른 숫자를 봤다.
    """

    def test_period_marker_matches_dart_naming(self):
        """정기보고서 정정은 이름에 기간이 붙는다: '[기재정정]반기보고서 (2026.06)'."""
        from dartlens.server import _REPRT_PERIOD_MONTH
        self.assertEqual(_REPRT_PERIOD_MONTH["11011"], "12")  # 사업보고서
        self.assertEqual(_REPRT_PERIOD_MONTH["11013"], "03")  # 1분기
        self.assertEqual(_REPRT_PERIOD_MONTH["11012"], "06")  # 반기
        self.assertEqual(_REPRT_PERIOD_MONTH["11014"], "09")  # 3분기

    def test_note_names_the_correction_filing(self):
        """무엇이 언제 정정됐는지, 원문을 어떻게 찾는지까지 줘야 확인이 가능하다."""
        from dartlens.server import _correction_note
        note = _correction_note({
            "rcept_dt": "20260814",
            "rcept_no": "20260814004258",
            "report_nm": "[기재정정]반기보고서 (2026.06)",
        })
        self.assertIn("2026-08-14", note)
        self.assertIn("20260814004258", note)
        self.assertIn("[기재정정]반기보고서 (2026.06)", note)
        self.assertIn("정정 반영본", note)

    def test_no_correction_no_noise(self):
        """정정이 없으면 아무 말도 하지 않는다. 항상 뜨는 경고는 무시된다."""
        from dartlens.server import _correction_note
        self.assertIsNone(_correction_note(None))
