"""corp code 캐시 진단(cache_diagnosis)/복구(repair_corp_code_cache) 테스트.

실제 사용자 캐시(~/.dartlens/cache/corpCode.xml)를 절대 건드리지 않도록
_corp_code.get_data_dir 를 항상 임시 디렉토리로 patch한다.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens import _corp_code

SAMPLE_XML = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<result>\n"
    "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
    "<corp_eng_name>Samsung</corp_eng_name><stock_code>005930</stock_code>"
    "<modify_date>20260101</modify_date></list>\n"
    "<list><corp_code>00164742</corp_code><corp_name>비상장테스트</corp_name>"
    "<corp_eng_name>Unlisted</corp_eng_name><stock_code></stock_code>"
    "<modify_date>20260101</modify_date></list>\n"
    "</result>"
).encode("utf-8")


class _TempCacheDirMixin:
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patch = patch.object(_corp_code, "get_data_dir", return_value=Path(self._tmpdir.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _cache_path(self) -> Path:
        return _corp_code._cache_path()


class CacheDiagnosisTests(_TempCacheDirMixin, unittest.TestCase):
    def test_missing_cache(self):
        diag = _corp_code.cache_diagnosis()
        self.assertFalse(diag["exists"])
        self.assertIsNone(diag["entry_count"])
        self.assertIsNone(diag["last_updated"])

    def test_fresh_valid_cache(self):
        self._cache_path().write_bytes(SAMPLE_XML)
        diag = _corp_code.cache_diagnosis()
        self.assertTrue(diag["exists"])
        self.assertTrue(diag["parseable"])
        self.assertEqual(diag["entry_count"], 2)
        self.assertTrue(diag["is_fresh"])
        self.assertIsNotNone(diag["last_updated"])

    def test_corrupted_cache_is_not_parseable(self):
        self._cache_path().write_bytes(b"not xml at all {{{")
        diag = _corp_code.cache_diagnosis()
        self.assertTrue(diag["exists"])
        self.assertFalse(diag["parseable"])
        self.assertEqual(diag["entry_count"], 0)

    def test_stale_cache_is_not_fresh(self):
        path = self._cache_path()
        path.write_bytes(SAMPLE_XML)
        old_time = time.time() - (8 * 24 * 3600)  # TTL(7일) 초과
        os.utime(path, (old_time, old_time))
        diag = _corp_code.cache_diagnosis()
        self.assertTrue(diag["exists"])
        self.assertTrue(diag["parseable"])
        self.assertFalse(diag["is_fresh"])


class RepairCorpCodeCacheTests(_TempCacheDirMixin, unittest.TestCase):
    def test_without_yes_touches_nothing(self):
        path = self._cache_path()
        path.write_bytes(SAMPLE_XML)

        result = _corp_code.repair_corp_code_cache(yes=False)

        self.assertFalse(result["repaired"])
        self.assertFalse(path.with_suffix(path.suffix + ".bak").exists())
        self.assertEqual(path.read_bytes(), SAMPLE_XML)

    def test_with_yes_backs_up_before_redownload(self):
        path = self._cache_path()
        path.write_bytes(SAMPLE_XML)

        async def fake_ensure_loaded(force_refresh=False):
            # 실제 재다운로드 대신 같은 내용을 다시 써서 "재다운로드"를 흉내낸다.
            path.write_bytes(SAMPLE_XML)

        with patch.object(_corp_code, "ensure_loaded", fake_ensure_loaded):
            result = _corp_code.repair_corp_code_cache(yes=True)

        self.assertTrue(result["repaired"])
        self.assertEqual(result["entry_count"], 2)
        bak = path.with_suffix(path.suffix + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_bytes(), SAMPLE_XML)

    def test_redownload_failure_is_reported_not_raised(self):
        path = self._cache_path()
        path.write_bytes(SAMPLE_XML)

        async def failing_ensure_loaded(force_refresh=False):
            raise RuntimeError("network down")

        with patch.object(_corp_code, "ensure_loaded", failing_ensure_loaded):
            result = _corp_code.repair_corp_code_cache(yes=True)

        self.assertFalse(result["repaired"])
        self.assertIn("message", result)
        # 실패해도 백업은 이미 만들어졌어야 한다 (삭제 전 보존 원칙).
        self.assertTrue(path.with_suffix(path.suffix + ".bak").exists())

    def test_backup_copy_failure_is_reported_not_raised(self):
        """shutil.copy2가 실패(디스크 꽉 참/권한 문제 등)해도 raw exception이 아니라
        {"repaired": False, ...}로 깨끗하게 끝나야 한다."""
        path = self._cache_path()
        path.write_bytes(SAMPLE_XML)

        with patch.object(_corp_code.shutil, "copy2", side_effect=OSError("disk full")):
            result = _corp_code.repair_corp_code_cache(yes=True)

        self.assertFalse(result["repaired"])
        self.assertIn("message", result)


if __name__ == "__main__":
    unittest.main()
