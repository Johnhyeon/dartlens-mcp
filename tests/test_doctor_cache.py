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


    # DART가 키 문제·한도 초과 등에서 돌려주는 형태. lxml로 잘 파싱되고 <list>만 없다.
DART_ERROR_XML = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
    "<result><status>013</status><message>조회된 데이타가 없습니다.</message></result>"
).encode("utf-8")


class ErrorResponseMustNotBecomeTheCache(_TempCacheDirMixin, unittest.TestCase):
    """실사용에서 확인된 문제(2026-08-13, 뉴질랜드 문의 조사 중 발견).

    DART가 zip 대신 에러 XML을 주면 예전 코드는 그걸 그대로 corpCode.xml로 저장했다.
    예외도 안 나고, 파싱도 되고, <list>만 없어서 '기업 0곳'짜리 캐시가 7일을 버텼다.
    그동안 회사 조회는 전부 실패하는데 doctor는 "최신 상태입니다"라고 말했고,
    캐시가 패키지 밖(~/.dartlens)에 있어 재설치로도 안 풀렸다.
    """

    def _run_with_response(self, raw: bytes):
        async def fake_get_bytes(endpoint, params=None, **kw):
            return raw

        with patch.object(_corp_code, "get_bytes", fake_get_bytes):
            import asyncio

            _corp_code._loaded_at = 0.0
            return asyncio.run(_corp_code.ensure_loaded(force_refresh=True))

    def test_error_xml_raises_instead_of_being_cached(self):
        with self.assertRaises(Exception):
            self._run_with_response(DART_ERROR_XML)
        self.assertFalse(self._cache_path().exists(), "에러 응답이 캐시로 남았다")

    def test_the_message_says_what_came_back(self):
        with self.assertRaises(Exception) as ctx:
            self._run_with_response(DART_ERROR_XML)
        self.assertIn("013", str(ctx.exception))

    def test_a_good_zip_is_still_cached(self):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("CORPCODE.xml", SAMPLE_XML)
        self._run_with_response(buf.getvalue())
        self.assertTrue(self._cache_path().exists())
        self.assertEqual(len(_corp_code._by_corp_code), 2)

    def test_an_already_poisoned_cache_is_re_downloaded(self):
        """옛 버전이 남긴 0건짜리 캐시에서 스스로 빠져나와야 한다 — 재설치로는 안 풀린다."""
        self._cache_path().write_bytes(DART_ERROR_XML)
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("CORPCODE.xml", SAMPLE_XML)

        async def fake_get_bytes(endpoint, params=None, **kw):
            return buf.getvalue()

        with patch.object(_corp_code, "get_bytes", fake_get_bytes):
            import asyncio

            _corp_code._loaded_at = 0.0
            asyncio.run(_corp_code.ensure_loaded())  # force_refresh 없이도 회복해야 한다
        self.assertEqual(len(_corp_code._by_corp_code), 2)

    def test_doctor_calls_an_empty_cache_a_failure(self):
        from dartlens import doctor

        self._cache_path().write_bytes(DART_ERROR_XML)
        check = doctor.check_corp_code_cache()
        self.assertEqual(check.status, "fail")


if __name__ == "__main__":
    unittest.main()
