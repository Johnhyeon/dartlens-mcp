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


if __name__ == "__main__":
    unittest.main(verbosity=2)
