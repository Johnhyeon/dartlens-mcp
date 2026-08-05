"""dartlens_status MCP 도구 테스트 — 라이선스 게이트(@safe_tool)를 의도적으로 안 거치는지 확인."""

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

from dartlens import diagnostics, server

_EMPTY_CACHE_DIAG = {
    "exists": False,
    "last_updated": None,
    "is_fresh": False,
    "entry_count": None,
    "parseable": None,
    "writable": True,
}
_EMPTY_CALL_STATUS = {"last_call_at": None, "last_status": None, "last_success_at": None}


class DartlensStatusToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_works_without_license_or_api_key(self):
        """is_licensed()를 patch하지 않고도 정상 텍스트가 나오는 것으로 safe_tool 게이트 미적용을 실증."""
        license_diag = diagnostics.LicenseDiagnosis(
            status="missing", error_code=diagnostics.DARTLENS_LICENSE_MISSING, message="라이선스가 없습니다."
        )
        api_diag = diagnostics.DartApiDiagnosis(
            status="missing", error_code=diagnostics.DART_API_KEY_MISSING, message="API 키가 없습니다."
        )

        with patch.object(diagnostics, "diagnose_license", return_value=license_diag), \
             patch.object(diagnostics, "diagnose_dart_api_key", return_value=api_diag), \
             patch.object(server, "cache_diagnosis", return_value=_EMPTY_CACHE_DIAG), \
             patch.object(server, "read_dart_call_status", return_value=_EMPTY_CALL_STATUS), \
             patch.object(diagnostics, "fetch_latest_pypi_version", new=AsyncMock(return_value=None)):
            result = await server.dartlens_status()

        self.assertIsInstance(result, str)
        self.assertIn("DartLens 상태", result)
        self.assertIn("라이선스가 없습니다", result)
        self.assertIn("API 키가 없습니다", result)
        self.assertNotIn("🔒", result)  # licensing.LOCKED_MESSAGE 로 대체되지 않았는지

    async def test_check_online_true_uses_online_diagnosis(self):
        online_diag = diagnostics.DartApiDiagnosis(status="valid", storage="env", key_tail_masked="****abcd")
        active_license = diagnostics.LicenseDiagnosis(status="active", license_id_masked="****1234")

        with patch.object(diagnostics, "diagnose_license", return_value=active_license), \
             patch.object(diagnostics, "diagnose_dart_api_key_online", new=AsyncMock(return_value=online_diag)) as mock_online, \
             patch.object(diagnostics, "diagnose_dart_api_key") as mock_offline, \
             patch.object(server, "cache_diagnosis", return_value=_EMPTY_CACHE_DIAG), \
             patch.object(server, "read_dart_call_status", return_value=_EMPTY_CALL_STATUS), \
             patch.object(diagnostics, "fetch_latest_pypi_version", new=AsyncMock(return_value=None)):
            result = await server.dartlens_status(check_online=True)

        mock_online.assert_awaited_once()
        mock_offline.assert_not_called()
        self.assertIn("정상", result)
        self.assertIn("실제 유효성 확인됨", result)

    async def test_unexpected_error_is_caught_not_raised(self):
        with patch.object(diagnostics, "diagnose_license", side_effect=RuntimeError("boom")):
            result = await server.dartlens_status()
        self.assertIn("⚠️ 상태 조회 중 오류", result)
        self.assertIn("RuntimeError", result)


if __name__ == "__main__":
    unittest.main()
