"""dartlens-doctor --json / --online 계약 테스트."""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx

from dartlens import diagnostics


class DiagnoseDartApiKeyOnlineTests(unittest.IsolatedAsyncioTestCase):
    """diagnose_dart_api_key_online()의 DART 응답 코드 분류. 네트워크는 항상 mock."""

    async def _run(self, *, mock_return=None, mock_side_effect=None):
        with patch.dict(os.environ, {"DART_API_KEY": "a" * 40}), \
             patch.object(diagnostics, "check_dart_key_online", new_callable=AsyncMock) as mock_check:
            if mock_side_effect is not None:
                mock_check.side_effect = mock_side_effect
            else:
                mock_check.return_value = mock_return
            return await diagnostics.diagnose_dart_api_key_online()

    async def test_000_is_valid(self):
        diag = await self._run(mock_return=("000", {"corp_name": "삼성전자"}))
        self.assertEqual(diag.status, "valid")
        self.assertIsNone(diag.error_code)

    async def test_013_is_valid_not_a_failure(self):
        diag = await self._run(mock_return=("013", {"message": "조회된 데이터가 없습니다"}))
        self.assertEqual(diag.status, "valid")

    async def test_020_is_rate_limited_and_distinct_from_invalid(self):
        diag = await self._run(mock_return=("020", {"message": "요청 제한"}))
        self.assertEqual(diag.status, "rate_limited")
        self.assertEqual(diag.error_code, diagnostics.DART_API_RATE_LIMITED)
        self.assertNotEqual(diag.status, "invalid")

    async def test_021_is_rate_limited(self):
        diag = await self._run(mock_return=("021", {"message": "조회 회사 수 초과"}))
        self.assertEqual(diag.status, "rate_limited")

    async def test_010_is_invalid(self):
        diag = await self._run(mock_return=("010", {"message": "등록되지 않은 키"}))
        self.assertEqual(diag.status, "invalid")
        self.assertEqual(diag.error_code, diagnostics.DART_API_KEY_INVALID)

    async def test_connect_error_is_network_unreachable_not_invalid(self):
        diag = await self._run(mock_side_effect=httpx.ConnectError("boom"))
        self.assertEqual(diag.status, "network_unreachable")
        self.assertEqual(diag.error_code, diagnostics.DART_NETWORK_UNREACHABLE)
        self.assertNotEqual(diag.status, "invalid")

    async def test_timeout_is_network_unreachable(self):
        diag = await self._run(mock_side_effect=httpx.TimeoutException("boom"))
        self.assertEqual(diag.status, "network_unreachable")

    async def test_no_key_leak_in_dict(self):
        key = "a" * 40
        diag = await self._run(mock_return=("000", {}))
        self.assertNotIn(key, json.dumps(diag.to_dict()))

    async def test_901_is_invalid_not_network_unreachable(self):
        """901(개인정보 보유기간 만료)은 계정 문제라 '서비스 문제, 당신 잘못 아님'으로 오도하면 안 됨."""
        diag = await self._run(mock_return=("901", {"message": "사용자 계정의 개인정보 보유기간이 만료되었습니다"}))
        self.assertEqual(diag.status, "invalid")
        self.assertNotEqual(diag.status, "network_unreachable")
        self.assertIn("개인정보 보유기간", diag.message)

    async def test_800_system_maintenance_is_network_unreachable(self):
        diag = await self._run(mock_return=("800", {"message": "시스템 점검"}))
        self.assertEqual(diag.status, "network_unreachable")


class CheckDartKeyOnlineTests(unittest.IsolatedAsyncioTestCase):
    """check_dart_key_online()이 DART의 비정상 응답(200이지만 JSON이 아님 등)도 httpx.HTTPError로
    통일해서 던지는지 — 그래야 기존 호출부들의 `except httpx.HTTPError` 폴백이 실제로 잡는다."""

    async def test_non_json_200_response_raises_http_error_not_raw_exception(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(side_effect=ValueError("not valid json"))
        with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=mock_resp)):
            with self.assertRaises(httpx.HTTPError):
                await diagnostics.check_dart_key_online("a" * 40)

    async def test_unexpected_json_shape_raises_http_error(self):
        """DART가 dict가 아닌 JSON(list 등)을 주면 .get() 호출에서 AttributeError가 나는데,
        이것도 httpx.HTTPError로 통일돼야 한다."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=["unexpected", "shape"])
        with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=mock_resp)):
            with self.assertRaises(httpx.HTTPError):
                await diagnostics.check_dart_key_online("a" * 40)


class DoctorMainCrashHandlingTests(unittest.TestCase):
    """run_diagnostics()/repair가 예상 못하게 터져도 raw traceback이 아니라 깨끗한 오류로 끝나는지."""

    def test_json_mode_reports_clean_error_instead_of_traceback(self):
        from dartlens import doctor

        buf = io.StringIO()
        with patch.object(doctor, "run_diagnostics", side_effect=RuntimeError("boom")), \
             patch.object(sys, "argv", ["dartlens-doctor", "--json"]), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                doctor.main()
        self.assertEqual(ctx.exception.code, 1)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["overall"], "fail")
        self.assertIn("RuntimeError", result["error"])

    def test_text_mode_reports_clean_error_instead_of_traceback(self):
        from dartlens import doctor

        buf_out, buf_err = io.StringIO(), io.StringIO()
        with patch.object(doctor, "run_diagnostics", side_effect=RuntimeError("boom")), \
             patch.object(sys, "argv", ["dartlens-doctor"]), \
             contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            with self.assertRaises(SystemExit) as ctx:
                doctor.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("RuntimeError", buf_err.getvalue())

    def test_repair_mode_reports_clean_error_instead_of_traceback(self):
        from dartlens import doctor

        buf = io.StringIO()
        with patch.object(doctor._corp_code, "repair_corp_code_cache", side_effect=RuntimeError("boom")), \
             patch.object(sys, "argv", ["dartlens-doctor", "--repair", "corp-code-cache", "--yes", "--json"]), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                doctor.main()
        self.assertEqual(ctx.exception.code, 1)
        result = json.loads(buf.getvalue())
        self.assertFalse(result["repaired"])
        self.assertIn("RuntimeError", result["message"])


class DoctorBuildReportTests(unittest.TestCase):
    """doctor.run_diagnostics()/build_report()가 유효한 JSON을 만들고 키 원문을 절대 담지 않는지."""

    def test_offline_report_shape_and_no_key_leak(self):
        from dartlens import doctor

        key = "b" * 40
        with patch.dict(os.environ, {"DART_API_KEY": key}):
            state = doctor.run_diagnostics(online=False)
            report = doctor.build_report(state, online=False)

        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(key, text)

        for top_key in (
            "schema_version", "product", "package_name", "installed_version",
            "overall", "checked_at", "online", "license", "targets",
            "dart_api", "corp_code_cache", "checks",
        ):
            self.assertIn(top_key, report)
        self.assertIn(report["overall"], ("ok", "degraded", "fail"))
        self.assertFalse(report["online"])
        self.assertEqual(report["dart_api"]["status"], "valid")
        self.assertIsInstance(report["checks"], list)

    def test_online_report_upgrades_dart_api_status(self):
        from dartlens import doctor

        key = "c" * 40
        with patch.dict(os.environ, {"DART_API_KEY": key}), \
             patch.object(diagnostics, "check_dart_key_online", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = ("020", {"message": "요청 제한"})
            state = doctor.run_diagnostics(online=True)
            report = doctor.build_report(state, online=True)

        self.assertTrue(report["online"])
        self.assertEqual(report["dart_api"]["status"], "rate_limited")
        self.assertEqual(report["dart_api"]["error_code"], diagnostics.DART_API_RATE_LIMITED)
        self.assertNotIn(key, json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
