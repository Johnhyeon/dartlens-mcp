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


if __name__ == "__main__":
    unittest.main()
