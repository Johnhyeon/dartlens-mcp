"""diagnostics.py 자격증명 진단 — DART API 키 / DartLens 라이선스 완전 분리 테스트."""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import diagnostics, licensing


def _fake_license_shaped_key() -> str:
    """진짜 서명 없이 '라이선스 키처럼 생긴(디코드 시 74바이트)' 문자열만 필요할 때(형태 판정 테스트용)."""
    return base64.b32encode(os.urandom(74)).decode()


class ResolveDartApiKeyTests(unittest.TestCase):
    def test_env_takes_priority_over_keychain_and_config(self):
        with patch.dict(os.environ, {"DART_API_KEY": "env-key-1234567890"}), \
             patch.object(diagnostics.keyring_helper, "load", return_value="keychain-key"):
            key, storage = diagnostics.resolve_dart_api_key(config_plaintext_key="config-key")
        self.assertEqual(key, "env-key-1234567890")
        self.assertEqual(storage, "env")

    def test_keychain_takes_priority_over_config(self):
        with patch.dict(os.environ, {"DART_API_KEY": ""}), \
             patch.object(diagnostics.keyring_helper, "load", return_value="keychain-key"):
            key, storage = diagnostics.resolve_dart_api_key(config_plaintext_key="config-key")
        self.assertEqual(key, "keychain-key")
        self.assertEqual(storage, "os-keychain")

    def test_config_plaintext_is_last_resort(self):
        with patch.dict(os.environ, {"DART_API_KEY": ""}), \
             patch.object(diagnostics.keyring_helper, "load", return_value=None):
            key, storage = diagnostics.resolve_dart_api_key(config_plaintext_key="config-key")
        self.assertEqual(key, "config-key")
        self.assertEqual(storage, "plaintext-config")

    def test_nothing_found(self):
        with patch.dict(os.environ, {"DART_API_KEY": ""}), \
             patch.object(diagnostics.keyring_helper, "load", return_value=None):
            key, storage = diagnostics.resolve_dart_api_key()
        self.assertIsNone(key)
        self.assertIsNone(storage)


class DiagnoseDartApiKeyTests(unittest.TestCase):
    def test_missing_when_backend_healthy(self):
        with patch.dict(os.environ, {"DART_API_KEY": ""}), \
             patch.object(diagnostics.keyring_helper, "load", return_value=None), \
             patch.object(diagnostics.keyring_helper, "backend_status", return_value=(True, "MockBackend")):
            diag = diagnostics.diagnose_dart_api_key()
        self.assertEqual(diag.status, "missing")
        self.assertEqual(diag.error_code, diagnostics.DART_API_KEY_MISSING)

    def test_storage_failed_when_backend_broken(self):
        """키가 없는 게 아니라 키체인 자체가 죽어있으면 missing이 아니라 storage_failed."""
        with patch.dict(os.environ, {"DART_API_KEY": ""}), \
             patch.object(diagnostics.keyring_helper, "load", return_value=None), \
             patch.object(diagnostics.keyring_helper, "backend_status", return_value=(False, "no backend")):
            diag = diagnostics.diagnose_dart_api_key()
        self.assertEqual(diag.status, "storage_failed")
        self.assertEqual(diag.error_code, diagnostics.DART_API_KEY_STORAGE_FAILED)

    def test_valid_dart_shaped_key(self):
        with patch.dict(os.environ, {"DART_API_KEY": "a" * 40}):
            diag = diagnostics.diagnose_dart_api_key()
        self.assertEqual(diag.status, "valid")
        self.assertEqual(diag.storage, "env")
        self.assertEqual(diag.key_tail_masked, "****aaaa")

    def test_license_key_in_api_field_is_invalid_with_cross_hint(self):
        fake_license = _fake_license_shaped_key()
        with patch.dict(os.environ, {"DART_API_KEY": fake_license}):
            diag = diagnostics.diagnose_dart_api_key()
        self.assertEqual(diag.status, "invalid")
        self.assertEqual(diag.error_code, diagnostics.DART_API_KEY_INVALID)
        self.assertIn("라이선스 키", diag.message)
        self.assertIn("dartlens-activate", diag.message)

    def test_to_dict_never_contains_raw_key(self):
        key = "b" * 40
        with patch.dict(os.environ, {"DART_API_KEY": key}):
            diag = diagnostics.diagnose_dart_api_key()
        text = json.dumps(diag.to_dict(), ensure_ascii=False)
        self.assertNotIn(key, text)
        self.assertIn("****", text)


class DiagnoseLicenseTests(unittest.TestCase):
    def test_missing(self):
        with patch.object(licensing, "stored_key", return_value=None):
            diag = diagnostics.diagnose_license()
        self.assertEqual(diag.status, "missing")
        self.assertEqual(diag.error_code, diagnostics.DARTLENS_LICENSE_MISSING)

    def test_api_key_in_license_field_is_invalid_with_cross_hint(self):
        fake_api_key = "c" * 40
        with patch.object(licensing, "stored_key", return_value=fake_api_key):
            diag = diagnostics.diagnose_license()
        self.assertEqual(diag.status, "invalid")
        self.assertEqual(diag.error_code, diagnostics.DARTLENS_LICENSE_INVALID)
        self.assertIn("DART API 키", diag.message)
        self.assertIn("dartlens-setup", diag.message)

    def test_active_when_verify_succeeds(self):
        with patch.object(licensing, "stored_key", return_value="whatever"), \
             patch.object(licensing, "verify_key", return_value={"valid": True, "license_id": "abcdef123456"}):
            diag = diagnostics.diagnose_license()
        self.assertEqual(diag.status, "active")
        self.assertEqual(diag.license_id_masked, "****3456")

    def test_to_dict_never_contains_raw_license_id(self):
        with patch.object(licensing, "stored_key", return_value="whatever"), \
             patch.object(licensing, "verify_key", return_value={"valid": True, "license_id": "abcdef123456"}):
            diag = diagnostics.diagnose_license()
        text = json.dumps(diag.to_dict(), ensure_ascii=False)
        self.assertNotIn("abcdef123456", text)


class StoredKeyCorruptionTests(unittest.TestCase):
    """license.key 파일이 손상(바이너리 등)돼도 stored_key()가 예외를 흘리지 않는지.

    diagnose_license()가 doctor.py에서 아무 try/except 없이 바로 호출되므로, 여기서
    UnicodeDecodeError가 새면 dartlens-doctor 전체가 raw traceback으로 죽는다.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = patch.dict(os.environ, {"DARTLENS_HOME": self._tmpdir.name}, clear=False)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_binary_license_file_returns_none_not_raises(self):
        license_path = Path(self._tmpdir.name) / "license.key"
        license_path.write_bytes(b"\xff\xfe\x00\x01\x80\x81corrupted-not-utf8")

        key = licensing.stored_key()

        self.assertIsNone(key)

    def test_diagnose_license_handles_corrupted_file_gracefully(self):
        license_path = Path(self._tmpdir.name) / "license.key"
        license_path.write_bytes(b"\xff\xfe\x00\x01\x80\x81corrupted-not-utf8")

        diag = diagnostics.diagnose_license()

        self.assertEqual(diag.status, "missing")


if __name__ == "__main__":
    unittest.main()
