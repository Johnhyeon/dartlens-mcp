"""오류 코드 상수 — 의존성 없는 최하위 모듈.

`diagnostics.py`와 `licensing.py`가 둘 다 이 값들을 참조해야 하는데, `diagnostics.py`는
`licensing.py`에 의존하므로(라이선스 검증 재사용) `licensing.py`가 반대로 `diagnostics.py`를
가져오면 순환 임포트가 된다. 코드값 자체는 어느 도메인 모듈에도 속하지 않는 공유 상수라
이 파일로 분리했다.
"""

from __future__ import annotations

DART_API_KEY_MISSING = "DART_API_KEY_MISSING"
DART_API_KEY_INVALID = "DART_API_KEY_INVALID"
DART_API_KEY_STORAGE_FAILED = "DART_API_KEY_STORAGE_FAILED"
DART_API_RATE_LIMITED = "DART_API_RATE_LIMITED"
DARTLENS_LICENSE_MISSING = "DARTLENS_LICENSE_MISSING"
DARTLENS_LICENSE_INVALID = "DARTLENS_LICENSE_INVALID"
DART_NETWORK_UNREACHABLE = "DART_NETWORK_UNREACHABLE"
CORP_CODE_CACHE_MISSING = "CORP_CODE_CACHE_MISSING"
CORP_CODE_CACHE_STALE = "CORP_CODE_CACHE_STALE"
