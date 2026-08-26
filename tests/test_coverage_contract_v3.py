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

    async def test_limit_is_reported_as_requested_range(self):
        text = await self._run({"total_count": 2894, "list": _rows(20)})
        meta = extract_meta(text)
        self.assertEqual(meta["coverage"]["requested"], {"unit": "item", "value": 20})
        self.assertEqual(meta["coverage"]["effective"], {"unit": "item", "value": 20})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
