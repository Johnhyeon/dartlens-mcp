"""메타 규약 v3 회귀 - DartLens.

DART는 조건에 맞는 공시가 몇 건인지 total_count 로 알려준다. 그런데 limit=20 이면
20건만 돌려주면서 응답 메타에는 `complete` 가 붙어 있었다. 2,894건 중 20건을 보고
"최근 1년 공시를 다 봤다"고 읽으면 그 판단은 통째로 틀린다.

본문에는 "표시 20건 / 전체 2894건" 안내가 이미 있었지만, 그 줄을 안 읽으면 그만이고
메타를 신뢰하는 소비자에게는 아무 신호도 가지 않았다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import _result_meta as rmeta
from dartlens import server


def extract_meta(text: str) -> dict:
    if rmeta.MARKER_START in text:
        payload = text.split(rmeta.MARKER_START, 1)[1].split(rmeta.MARKER_END, 1)[0].strip()
        return json.loads(payload)
    return json.loads(text)["_meta"]


def _rows(n: int) -> list[dict]:
    return [
        {
            "rcept_no": f"20260826{i:06d}",
            "rcept_dt": "20260826",
            "corp_name": "삼성전자",
            "report_nm": f"주요사항보고서 {i}",
            "corp_code": "00126380",
            "stock_code": "005930",
        }
        for i in range(n)
    ]


class DisclosureCoverageTests(unittest.IsolatedAsyncioTestCase):
    """DL-01. 목록이 잘렸는지가 메타에 있는가."""

    async def _run(self, payload, **kwargs):
        opts = dict(corp_code="00126380", bgn_de="20250826", end_de="20260826", limit=20)
        opts.update(kwargs)
        with patch.object(server, "_fetch_disclosure_list", AsyncMock(return_value=payload)):
            return await server.list_disclosures(**opts)

    async def test_truncated_listing_is_partial(self):
        text = await self._run({"total_count": 2894, "list": _rows(20)})
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["returned_count"], 20)
        self.assertEqual(meta["coverage"]["total_count"], 2894)
        self.assertIs(meta["coverage"]["truncated"], True)
        self.assertIs(meta["coverage"]["coverage_complete"], False)
        self.assertEqual(meta["coverage"]["reason"], "pagination")
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_truncated_listing_tells_how_to_narrow(self):
        """본문의 기존 '표시 N건 / 전체 M건' 안내는 그대로 두고 한 줄만 더한다."""
        text = await self._run({"total_count": 2894, "list": _rows(20)})
        self.assertIn("표시 20건 / 전체 2894건", text)
        self.assertIn("기간", text)
        self.assertIn("kind", text)

    async def test_full_listing_is_complete(self):
        text = await self._run({"total_count": 8, "list": _rows(8)})
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["returned_count"], 8)
        self.assertEqual(meta["coverage"]["total_count"], 8)
        self.assertIs(meta["coverage"]["truncated"], False)
        self.assertIs(meta["coverage"]["coverage_complete"], True)
        self.assertIsNone(meta["coverage"]["reason"])
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_empty_listing_is_none_but_not_a_lie(self):
        """0건은 '못 봤다'가 아니라 '없다'다. 원천이 0건이라고 말해줬다."""
        text = await self._run({"status": "013", "total_count": 0, "list": []})
        meta = extract_meta(text)
        self.assertEqual(meta["data_completeness"], "none")
        self.assertEqual(meta["coverage"]["returned_count"], 0)
        self.assertIs(meta["coverage"]["coverage_complete"], True)

    async def test_missing_total_count_does_not_claim_complete(self):
        """원천이 전체 건수를 안 주면 다 봤는지 알 수 없다. 모른다고 적는다."""
        text = await self._run({"list": _rows(20)})
        meta = extract_meta(text)
        self.assertIsNone(meta["coverage"]["total_count"])
        self.assertIs(meta["coverage"]["coverage_complete"], False)
        self.assertEqual(meta["coverage"]["reason"], "unknown")
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_requested_preserves_the_whole_query(self):
        """메타만 받은 소비자가 이 조회를 그대로 재현할 수 있어야 한다.

        limit 만 남기면 어느 기간의 어떤 유형을 본 것인지 복원할 수 없다.
        특히 공시 유형은 결과 본문에도 코드가 아니라 라벨로만 나온다.
        """
        text = await self._run(
            {"total_count": 2894, "list": _rows(20)}, kind="material"
        )
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {
            "bgn_de": "20250826",
            "end_de": "20260826",
            "kind": "material",
            "limit": 20,
        })

    async def test_requested_reflects_days_shorthand(self):
        """days 로 물어도 실제로 조회한 날짜 구간이 남아야 한다."""
        with patch.object(server, "_fetch_disclosure_list",
                          AsyncMock(return_value={"total_count": 3, "list": _rows(3)})):
            text = await server.list_disclosures(corp_code="00126380", days=30, limit=20)
        req = extract_meta(text)["coverage"]["requested"]
        self.assertEqual(req["limit"], 20)
        self.assertEqual(req["kind"], "all")
        self.assertRegex(req["bgn_de"], r"^\d{8}$")
        self.assertRegex(req["end_de"], r"^\d{8}$")

    async def test_body_table_shape_is_unchanged(self):
        text = await self._run({"total_count": 8, "list": _rows(8)})
        self.assertIn("| 접수일 | 회사 | 보고서명 | rcept_no | 비고 |", text)
        self.assertIn("_rcept_no는 향후 get_disclosure_detail 도구의 입력값으로 사용됩니다._", text)
# ---------------------------------------------------------------------------
# DL-02. 본문 검색이 몇 건 중 몇 건을 보여준 것인가
# ---------------------------------------------------------------------------


def _doc_with(keyword: str, times: int, filler: int = 800) -> str:
    """키워드가 정확히 times 번 나오는 본문."""
    chunk = "가" * filler
    return chunk + (keyword + chunk).join([""] * (times + 1)) if times else chunk


class FindMatchCoverageTests(unittest.IsolatedAsyncioTestCase):
    """5건만 만들어 놓고 "5건"이라고 적으면, 17건 중 5건을 본 사람이
    5건이 전부라고 읽는다. 세는 것과 보여주는 것을 나눠야 한다.
    """

    async def _run(self, text, keyword):
        with patch.object(server, "_fetch_document_zip", AsyncMock(return_value=b"")),              patch.object(server, "_parse_document_zip", return_value=(["doc.xml"], text)):
            return await server.get_disclosure_detail(
                rcept_no="20260826000001", find=keyword
            )

    async def test_truncated_matches_are_partial(self):
        text = await self._run(_doc_with("조기상환", 17), "조기상환")
        meta = extract_meta(text)
        self.assertEqual(meta["match_coverage"], {
            "keyword": "조기상환",
            "total_matches": 17,
            "displayed_matches": 5,
            "truncated": True,
            "coverage_complete": False,
        })
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_body_reports_total_not_displayed_count(self):
        """본문에 적히는 건수가 전체여야 한다. 표시 건수를 전체로 적으면 안 된다."""
        text = await self._run(_doc_with("조기상환", 17), "조기상환")
        self.assertIn("17건", text)

    async def test_all_matches_shown_is_complete(self):
        text = await self._run(_doc_with("조기상환", 3), "조기상환")
        meta = extract_meta(text)
        self.assertEqual(meta["match_coverage"]["total_matches"], 3)
        self.assertEqual(meta["match_coverage"]["displayed_matches"], 3)
        self.assertIs(meta["match_coverage"]["truncated"], False)
        self.assertIs(meta["match_coverage"]["coverage_complete"], True)
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_exactly_five_matches_is_not_truncated(self):
        """경계값. 5건이면 다 보여준 것이지 잘린 게 아니다."""
        text = await self._run(_doc_with("조기상환", 5), "조기상환")
        meta = extract_meta(text)
        self.assertEqual(meta["match_coverage"]["total_matches"], 5)
        self.assertIs(meta["match_coverage"]["truncated"], False)
        self.assertEqual(meta["data_completeness"], "complete")

    async def test_zero_matches_does_not_confirm_absence(self):
        """0건은 '본문에 없다'가 아니라 '이 키워드로는 못 찾았다'다."""
        text = await self._run(_doc_with("조기상환", 0), "배당금")
        meta = extract_meta(text)
        self.assertEqual(meta["match_coverage"]["total_matches"], 0)
        self.assertIs(meta["match_coverage"]["coverage_complete"], False)
        self.assertIs(meta["absence_confirmed"], False)
        self.assertEqual(meta["data_completeness"], "partial")

    async def test_zero_match_warning_is_kept(self):
        """표 텍스트 추출 누락 경고는 그대로 유지한다."""
        text = await self._run(_doc_with("조기상환", 0), "배당금")
        meta = extract_meta(text)
        self.assertTrue(any("표 안에 있어" in w for w in meta["warnings"]), meta["warnings"])

    def test_counting_is_separate_from_rendering(self):
        """전체 위치를 세는 일과 스니펫을 만드는 일은 따로다.

        전부에 스니펫을 붙이면 흔한 단어 하나에 응답이 폭발한다.
        """
        doc = _doc_with("조기상환", 17)
        positions = server._find_all_matches(doc, "조기상환")
        self.assertEqual(len(positions), 17)
        self.assertEqual(len(server._find_matches(doc, "조기상환")), 5)
        picked = server._find_matches(doc, "조기상환", positions[:2])
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0]["pos"], positions[0])
# ---------------------------------------------------------------------------
# DL-03/04. 연결과 별도가 섞였는가 · 이 숫자가 정정 반영본인가
# ---------------------------------------------------------------------------


def _acct(fs_div, account_nm, amount, ord_="1", currency="KRW"):
    return {
        "rcept_no": "20260814003699",
        "corp_code": "00126380",
        "stock_code": "005930",
        "fs_div": fs_div,
        "fs_nm": "연결재무제표" if fs_div == "CFS" else "재무제표",
        "sj_div": "IS",
        "sj_nm": "손익계산서",
        "account_nm": account_nm,
        "ord": ord_,
        "currency": currency,
        "thstrm_nm": "제 58 기 반기",
        "thstrm_amount": amount,
        "thstrm_add_amount": amount,
        "frmtrm_nm": "제 57 기 반기",
        "frmtrm_amount": amount,
        "frmtrm_add_amount": amount,
    }


class FinancialScopeTests(unittest.IsolatedAsyncioTestCase):
    """연결(CFS)과 별도(OFS)를 더하면 그 회사는 존재하지 않는 회사가 된다.

    fnlttSinglAcnt.json 은 두 범위를 한 응답에 같이 준다. 표는 나눠 그리지만
    메타에는 그 사실이 없어서, 행을 그대로 집계하는 소비자가 합산해 버린다.
    """

    async def _major(self, rows, correction=None, correction_raises=None):
        find = (AsyncMock(side_effect=correction_raises) if correction_raises
                else AsyncMock(return_value=correction))
        with patch.object(server, "_fetch_major_accounts", AsyncMock(return_value={"list": rows})),              patch.object(server, "find_correction", find):
            return await server.get_major_accounts(
                corp_code="00126380", bsns_year=2026, reprt_code="H1"
            )

    async def test_mixed_scope_is_reported_and_warned(self):
        rows = [_acct("CFS", "매출액", "1000"), _acct("OFS", "매출액", "600")]
        text = await self._major(rows)
        meta = extract_meta(text)
        self.assertEqual(meta["financial_scope"], {
            "scopes_present": ["CFS", "OFS"],
            "preferred_scope": "CFS",
            "scope_mixed_in_response": True,
            "currency": "KRW",
        })
        self.assertIn("합산", text)

    async def test_single_scope_is_not_flagged_as_mixed(self):
        text = await self._major([_acct("CFS", "매출액", "1000")])
        meta = extract_meta(text)
        self.assertEqual(meta["financial_scope"]["scopes_present"], ["CFS"])
        self.assertIs(meta["financial_scope"]["scope_mixed_in_response"], False)
        self.assertNotIn("합산", text)

    async def test_ofs_only_prefers_ofs(self):
        """연결이 없는 회사도 있다. 그때 preferred 를 CFS 로 적으면 거짓이다."""
        text = await self._major([_acct("OFS", "매출액", "600")])
        meta = extract_meta(text)
        self.assertEqual(meta["financial_scope"]["preferred_scope"], "OFS")

    async def test_currency_is_taken_from_rows(self):
        text = await self._major([_acct("CFS", "매출액", "1000", currency="USD")])
        meta = extract_meta(text)
        self.assertEqual(meta["financial_scope"]["currency"], "USD")


class FilingStateTests(unittest.IsolatedAsyncioTestCase):
    """정정 확인에 실패한 것과 정정이 없는 것은 다르다.

    _fetch_corrections 가 조회 실패를 빈 목록으로 삼키면 둘이 같아진다. 그러면
    정정된 보고서를 "정정 없음"으로 보여주게 된다.
    """

    async def _major(self, **kw):
        return await FinancialScopeTests._major(self, [_acct("CFS", "매출액", "1000")], **kw)

    async def test_no_correction_is_checked_and_clean(self):
        text = await self._major(correction=None)
        meta = extract_meta(text)
        self.assertEqual(meta["filing_state"], {
            "business_year": "2026",
            "report_code": "11012",
            "filing_date": "2026-08-14",
            "correction_checked": True,
            "correction_applied": False,
            "latest_correction_date": None,
        })

    async def test_applied_correction_records_its_date(self):
        text = await self._major(correction={
            "rcept_dt": "20260814",
            "rcept_no": "20260814004258",
            "report_nm": "[기재정정]반기보고서 (2026.06)",
        })
        meta = extract_meta(text)
        self.assertIs(meta["filing_state"]["correction_applied"], True)
        self.assertEqual(meta["filing_state"]["latest_correction_date"], "2026-08-14")
        self.assertIn("정정 반영본", text)

    async def test_failed_check_is_not_reported_as_clean(self):
        text = await self._major(correction_raises=RuntimeError("DART 응답 없음"))
        meta = extract_meta(text)
        self.assertIs(meta["filing_state"]["correction_checked"], False)
        self.assertIs(meta["filing_state"]["correction_applied"], False)
        self.assertTrue(
            any("정정" in w and "확인" in w for w in meta["warnings"]), meta["warnings"]
        )

    async def test_correction_lookup_failure_does_not_kill_the_tool(self):
        """수치는 받았다. 정정 확인만 실패했다고 표를 통째로 버리면 안 된다."""
        text = await self._major(correction_raises=RuntimeError("DART 응답 없음"))
        self.assertIn("주요계정", text)
        self.assertIn("매출액", text)


class FullFinancialScopeTests(unittest.IsolatedAsyncioTestCase):
    """get_full_financial 은 fs_div 하나만 가져온다. 섞일 수 없다는 사실도 적는다."""

    async def _run(self, fs_div, rows):
        with patch.object(server, "_fetch_full_financial", AsyncMock(return_value={"list": rows})),              patch.object(server, "find_correction", AsyncMock(return_value=None)):
            return await server.get_full_financial(
                corp_code="00126380", bsns_year=2026, reprt_code="H1",
                fs_div=fs_div, sj_div="IS",
            )

    async def test_single_scope_request_is_never_mixed(self):
        text = await self._run("OFS", [_acct("OFS", "매출액", "600")])
        meta = extract_meta(text)
        self.assertEqual(meta["financial_scope"]["scopes_present"], ["OFS"])
        self.assertEqual(meta["financial_scope"]["preferred_scope"], "OFS")
        self.assertIs(meta["financial_scope"]["scope_mixed_in_response"], False)

    async def test_rows_without_fs_div_use_the_requested_scope(self):
        """실측: fnlttSinglAcntAll.json 행에는 fs_div 가 아예 없다(141행 전부 None).

        이 엔드포인트는 요청 인자로 범위를 가르므로, 행에 표기가 없다고
        scopes_present 를 비워 두면 "무슨 범위인지 모른다"로 읽힌다. 실제로는 안다.
        """
        rows = [{k: v for k, v in _acct("OFS", "매출액", "600").items() if k != "fs_div"}]
        text = await self._run("OFS", rows)
        meta = extract_meta(text)
        self.assertEqual(meta["financial_scope"]["scopes_present"], ["OFS"])
        self.assertEqual(meta["financial_scope"]["preferred_scope"], "OFS")
        self.assertIs(meta["financial_scope"]["scope_mixed_in_response"], False)

    async def test_filing_state_present(self):
        text = await self._run("CFS", [_acct("CFS", "매출액", "1000")])
        meta = extract_meta(text)
        self.assertIs(meta["filing_state"]["correction_checked"], True)
        self.assertEqual(meta["filing_state"]["business_year"], "2026")


if __name__ == "__main__":
    unittest.main(verbosity=2)
