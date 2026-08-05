"""Manager 계약 — dartlens-setup / dartlens-activate 비대화형(--non-interactive/--json) 경로.

주의: 이 테스트는 실제 OS 키체인이나 실제 Claude config 파일(~/.claude.json 등)을
절대 건드리지 않는다 — keyring_helper.save 와 _write_config_entry 는 항상 mock 처리하고,
라이선스 관련 테스트는 DARTLENS_HOME 을 임시 디렉토리로 돌려서 실제 license.key 를 보호한다.
"""

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import diagnostics, licensing, setup_claude


def _fake_license_shaped_key() -> str:
    return base64.b32encode(os.urandom(74)).decode()


class RunSetupNoninteractiveTests(unittest.TestCase):
    def _patch_online(self, status="000", data=None):
        return patch.object(
            diagnostics, "check_dart_key_online", new=AsyncMock(return_value=(status, data or {}))
        )

    def test_success_stores_key_and_writes_only_given_targets(self):
        with self._patch_online("000", {"corp_name": "삼성전자"}), \
             patch.object(setup_claude.keyring_helper, "save", return_value="MockBackend") as mock_save, \
             patch.object(setup_claude, "_write_config_entry") as mock_write:
            result = setup_claude.run_setup_noninteractive(
                api_key="a" * 40, targets=["claude-code"], command="dartlens",
            )

        self.assertTrue(result["api_key_saved"])
        self.assertEqual(result["storage"], "os-keychain")
        self.assertEqual(result["targets"], ["claude-code"])
        self.assertTrue(result["validated_online"])
        self.assertNotIn("a" * 40, json.dumps(result))
        mock_save.assert_called_once()
        mock_write.assert_called_once()

    def test_empty_key_is_missing_when_nothing_stored(self):
        with patch.object(setup_claude.keyring_helper, "load", return_value=None):
            with self.assertRaises(setup_claude.SetupError) as ctx:
                setup_claude.run_setup_noninteractive(api_key="   ", targets=[])
        self.assertEqual(ctx.exception.error_code, diagnostics.DART_API_KEY_MISSING)

    def test_empty_key_reuses_stored_keychain_key(self):
        """키를 안 줘도 키체인에 이미 저장된 키가 있으면 재입력 없이 그걸로 진행한다."""
        with self._patch_online("000", {"corp_name": "삼성전자"}), \
             patch.object(setup_claude.keyring_helper, "load", return_value="a" * 40), \
             patch.object(setup_claude.keyring_helper, "save", return_value="MockBackend") as mock_save, \
             patch.object(setup_claude, "_write_config_entry") as mock_write:
            result = setup_claude.run_setup_noninteractive(api_key="", targets=["claude-desktop"])
        self.assertTrue(result["api_key_saved"])
        mock_save.assert_called_once_with("a" * 40)
        mock_write.assert_called_once()

    def test_license_key_in_api_field_rejected_before_network_call(self):
        fake_license = _fake_license_shaped_key()
        with self._patch_online() as _patched, \
             patch.object(setup_claude, "_write_config_entry") as mock_write:
            with self.assertRaises(setup_claude.SetupError) as ctx:
                setup_claude.run_setup_noninteractive(api_key=fake_license, targets=[])
        self.assertEqual(ctx.exception.error_code, diagnostics.DART_API_KEY_INVALID)
        self.assertIn("라이선스", ctx.exception.message)
        mock_write.assert_not_called()

    def test_rate_limited_is_distinct_error_code(self):
        with self._patch_online("020", {"message": "제한"}), \
             patch.object(setup_claude, "_write_config_entry"):
            with self.assertRaises(setup_claude.SetupError) as ctx:
                setup_claude.run_setup_noninteractive(api_key="a" * 40, targets=[])
        self.assertEqual(ctx.exception.error_code, diagnostics.DART_API_RATE_LIMITED)

    def test_dart_rejected_key_is_invalid(self):
        with self._patch_online("010", {"message": "등록되지 않은 키"}):
            with self.assertRaises(setup_claude.SetupError) as ctx:
                setup_claude.run_setup_noninteractive(api_key="a" * 40, targets=[])
        self.assertEqual(ctx.exception.error_code, diagnostics.DART_API_KEY_INVALID)

    def test_keyring_failure_without_consent_fails_closed(self):
        with self._patch_online("000", {}), \
             patch.object(
                 setup_claude.keyring_helper, "save",
                 side_effect=setup_claude.keyring_helper.KeyringUnavailableError("no backend"),
             ), \
             patch.object(setup_claude, "_write_config_entry") as mock_write:
            with self.assertRaises(setup_claude.SetupError) as ctx:
                setup_claude.run_setup_noninteractive(api_key="a" * 40, targets=["claude-code"])
        self.assertEqual(ctx.exception.error_code, diagnostics.DART_API_KEY_STORAGE_FAILED)
        mock_write.assert_not_called()

    def test_keyring_failure_with_explicit_consent_falls_back_to_plaintext(self):
        with self._patch_online("000", {}), \
             patch.object(
                 setup_claude.keyring_helper, "save",
                 side_effect=setup_claude.keyring_helper.KeyringUnavailableError("no backend"),
             ), \
             patch.object(setup_claude, "_write_config_entry") as mock_write:
            result = setup_claude.run_setup_noninteractive(
                api_key="a" * 40, targets=["claude-code"], plaintext_consent=True,
            )
        self.assertTrue(result["api_key_saved"])
        self.assertEqual(result["storage"], "plaintext-config")
        mock_write.assert_called_once()
        _, kwargs = mock_write.call_args
        self.assertTrue(kwargs["plaintext"])


class TryReuseStoredKeyTests(unittest.TestCase):
    """대화형 dartlens-setup이 키체인에 이미 저장된 키를 재입력 없이 재사용하는 경로."""

    def test_returns_none_when_nothing_stored(self):
        with patch.object(setup_claude.keyring_helper, "load", return_value=None):
            self.assertIsNone(setup_claude._try_reuse_stored_key())

    def test_returns_key_when_stored_key_validates(self):
        with patch.object(setup_claude.keyring_helper, "load", return_value="a" * 40), \
             patch.object(setup_claude, "validate_key", return_value=(True, "검증 성공 — 삼성전자")):
            self.assertEqual(setup_claude._try_reuse_stored_key(), "a" * 40)

    def test_returns_none_when_stored_key_fails_validation(self):
        """저장된 키가 더 이상 유효하지 않으면 재사용을 포기하고 프롬프트로 폴백하도록 None."""
        with patch.object(setup_claude.keyring_helper, "load", return_value="a" * 40), \
             patch.object(setup_claude, "validate_key", return_value=(False, "DART 응답 [010]: 등록되지 않은 키")):
            self.assertIsNone(setup_claude._try_reuse_stored_key())


class ActivateCliTests(unittest.TestCase):
    """licensing.activate_cli()의 --stdin/--json 경로. license.key는 임시 DARTLENS_HOME에만 쓴다."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = patch.dict(os.environ, {"DARTLENS_HOME": self._tmpdir.name}, clear=False)
        self._env_patch.start()
        licensing._licensed_cache = False

    def tearDown(self):
        self._env_patch.stop()
        self._tmpdir.cleanup()
        licensing._licensed_cache = False

    def _run_cli(self, argv):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["dartlens-activate", *argv]), contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                licensing.activate_cli()
        return ctx.exception.code, buf.getvalue()

    def test_dart_api_key_in_license_field_json(self):
        key = "a" * 40
        code, out = self._run_cli([key, "--json"])
        result = json.loads(out)
        self.assertFalse(result["license_activated"])
        self.assertEqual(result["error_code"], "DARTLENS_LICENSE_INVALID")
        self.assertIn("DART API 키", result["message"])
        self.assertEqual(code, 1)
        self.assertNotIn(key, out)

    def test_dart_api_key_in_license_field_human_output_shows_cross_hint(self):
        code, out = self._run_cli(["a" * 40])
        self.assertIn("DART API 키", out)
        self.assertEqual(code, 1)

    def test_stdin_mode_reads_key_without_touching_argv(self):
        fake_stdin = io.StringIO("not-a-real-key\n")
        with patch.object(sys, "stdin", fake_stdin):
            code, out = self._run_cli(["--stdin", "--json"])
        result = json.loads(out)
        self.assertFalse(result["license_activated"])
        self.assertEqual(code, 1)

    def test_successful_activation_json(self):
        with patch.object(licensing, "verify_key", return_value={"valid": True, "license_id": "abc123456789"}):
            code, out = self._run_cli(["FAKEKEY", "--json"])
        result = json.loads(out)
        self.assertTrue(result["license_activated"])
        self.assertEqual(result["license_id_masked"], "****6789")
        self.assertEqual(code, 0)

    def test_empty_key_json_reports_missing(self):
        # _prompt_key()는 tty에서만 입력을 기다린다 — 테스트 실행 환경이 우연히 tty여도
        # 블로킹하지 않도록 명시적으로 None(비대화형)을 강제한다.
        with patch.object(licensing, "is_licensed", return_value=False), \
             patch.object(licensing, "_prompt_key", return_value=None):
            code, out = self._run_cli(["--json"])
        result = json.loads(out)
        self.assertFalse(result["license_activated"])
        self.assertEqual(result["error_code"], "DARTLENS_LICENSE_MISSING")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
