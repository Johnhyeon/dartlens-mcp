"""DART API 키 / DartLens 라이선스 키 진단 — 두 자격증명을 완전히 분리해서 다룬다.

doctor.py(사람용 출력), CLI JSON 계약(setup/activate --json), MCP 도구
`dartlens_status`가 모두 이 모듈 하나를 통해 진단한다 — "키가 있다/없다/유효하다"의
판정 로직이 여러 곳에 흩어지지 않도록 하는 단일 소스.

DART 라이트 엔드포인트 호출(`check_dart_key_online`)도 여기서 소유한다.
`setup_claude.py`의 자체 키 검증과 `dartlens-doctor --online`이 같은 구현을 공유한다.

키 원문은 이 모듈이 반환하는 어떤 dict/dataclass에도 절대 담기지 않는다 — 항상
`licensing.mask_tail()`로 마지막 4자리만 남긴다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from dartlens import _keyring as keyring_helper
from dartlens import licensing
from dartlens._cache import cached

# 오류 코드 — 공유 상수는 _error_codes.py 소유. 여기서 재노출해서 기존
# `diagnostics.DART_API_KEY_MISSING` 형태 호출부(doctor.py, setup_claude.py)를 그대로 둔다.
from dartlens._error_codes import (  # noqa: F401
    DART_API_KEY_MISSING,
    DART_API_KEY_INVALID,
    DART_API_KEY_STORAGE_FAILED,
    DART_API_RATE_LIMITED,
    DARTLENS_LICENSE_MISSING,
    DARTLENS_LICENSE_INVALID,
    DART_NETWORK_UNREACHABLE,
    CORP_CODE_CACHE_MISSING,
    CORP_CODE_CACHE_STALE,
)

# Manager 공통 계약(LeetKit Manager Program Requirements 3.1) 최상위 필드 상수.
# StockLens/TelegramLens와 이름을 반드시 맞출 것 — 여기서 임의로 새 이름을 만들면
# Manager가 Lens별 파서를 따로 둬야 한다.
SCHEMA_VERSION = 1
PRODUCT = "dartlens"
PACKAGE_NAME = "dartlens-mcp"


def _version_gt(latest: str, current: str) -> bool:
    """semver 비교. 실패 시 단순 문자열 비교 fallback."""
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        return bool(latest) and latest != current


# ---------------------------------------------------------------------------
# 진단 결과 모델
# ---------------------------------------------------------------------------


@dataclass
class DartApiDiagnosis:
    status: str  # valid | missing | invalid | storage_failed | rate_limited | network_unreachable
    storage: str | None = None  # env | os-keychain | plaintext-config
    key_tail_masked: str | None = None
    error_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        d: dict = {"status": self.status}
        if self.storage is not None:
            d["storage"] = self.storage
        if self.key_tail_masked is not None:
            d["key_tail_masked"] = self.key_tail_masked
        if self.error_code is not None:
            d["error_code"] = self.error_code
        if self.message:
            d["message"] = self.message
        return d


@dataclass
class LicenseDiagnosis:
    status: str  # active | missing | invalid
    license_id_masked: str | None = None
    error_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        d: dict = {"status": self.status}
        if self.license_id_masked is not None:
            d["license_id_masked"] = self.license_id_masked
        if self.error_code is not None:
            d["error_code"] = self.error_code
        if self.message:
            d["message"] = self.message
        return d


# ---------------------------------------------------------------------------
# DART API 키 진단
# ---------------------------------------------------------------------------


def resolve_dart_api_key(*, config_plaintext_key: str | None = None) -> tuple[str | None, str | None]:
    """키 값과 출처를 우선순위대로 판정. env > os-keychain > config(plaintext).

    config_plaintext_key: doctor.py가 Claude 설정 JSON의 mcpServers.dartlens.env.DART_API_KEY를
    미리 파싱해서 넘겨준 값 (이 모듈은 config 파일을 직접 읽지 않는다 — 단일 책임).
    """
    env_key = (os.environ.get("DART_API_KEY") or "").strip()
    if env_key:
        return env_key, "env"

    try:
        stored = (keyring_helper.load() or "").strip()
    except Exception:
        stored = ""
    if stored:
        return stored, "os-keychain"

    if config_plaintext_key and config_plaintext_key.strip():
        return config_plaintext_key.strip(), "plaintext-config"

    return None, None


def diagnose_dart_api_key(*, config_plaintext_key: str | None = None) -> DartApiDiagnosis:
    """오프라인 진단 — 존재/저장소/형태(라이선스 키 오형 교차검사)만 판정. 네트워크 호출 없음."""
    key, storage = resolve_dart_api_key(config_plaintext_key=config_plaintext_key)

    if not key:
        ok, reason = keyring_helper.backend_status()
        if not ok:
            return DartApiDiagnosis(
                status="storage_failed",
                error_code=DART_API_KEY_STORAGE_FAILED,
                message=f"OS 키체인을 사용할 수 없어 키를 읽거나 저장할 수 없습니다 — {reason}",
            )
        return DartApiDiagnosis(
            status="missing",
            error_code=DART_API_KEY_MISSING,
            message=(
                "DART API 키가 없습니다. `dartlens-setup` 으로 등록하세요. "
                "(키가 없다면 https://opendart.fss.or.kr 에서 무료 발급)"
            ),
        )

    if licensing.looks_like_license_shape(key):
        return DartApiDiagnosis(
            status="invalid",
            storage=storage,
            key_tail_masked=licensing.mask_tail(key),
            error_code=DART_API_KEY_INVALID,
            message=licensing.CROSS_HINT_LICENSE_IN_API_KEY_FIELD,
        )

    return DartApiDiagnosis(status="valid", storage=storage, key_tail_masked=licensing.mask_tail(key))


# DART company.json — 삼성전자(00126380)는 항상 존재하는 안정적 corp_code라
# "가벼운 엔드포인트 1회 호출"로 키 유효성만 확인하는 용도에 적합하다.
_VALIDATE_URL = "https://opendart.fss.or.kr/api/company.json"
_VALIDATE_CORP_CODE = "00126380"

RATE_LIMIT_CODES = {"020", "021"}
# DART 서비스 자체 문제 — 키 불량으로 오판하면 안 됨
SERVICE_ISSUE_CODES = {"800", "900", "901"}
SUCCESS_CODES = {"000", "013"}  # 013 = 조회 결과 없음. 연결 성공으로 취급.


async def check_dart_key_online(api_key: str) -> tuple[str, dict]:
    """DART 가벼운 엔드포인트 1회 호출 → (status_code, raw_json).

    네트워크 예외(httpx.*)는 그대로 전파 — 호출자가 network_unreachable 로 매핑한다.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _VALIDATE_URL,
            params={"crtfc_key": api_key, "corp_code": _VALIDATE_CORP_CODE},
        )
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("status", "")).strip(), data


async def diagnose_dart_api_key_online(*, config_plaintext_key: str | None = None) -> DartApiDiagnosis:
    """오프라인 진단이 valid일 때만 실제로 DART를 호출해 검증을 업그레이드한다.

    missing/invalid/storage_failed는 온라인 확인이 의미 없으므로 그대로 반환한다.
    """
    base = diagnose_dart_api_key(config_plaintext_key=config_plaintext_key)
    if base.status != "valid":
        return base

    key, storage = resolve_dart_api_key(config_plaintext_key=config_plaintext_key)
    assert key is not None  # base.status == "valid" 이면 key는 반드시 있음

    try:
        code, data = await check_dart_key_online(key)
    except httpx.TimeoutException:
        return DartApiDiagnosis(
            status="network_unreachable",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_NETWORK_UNREACHABLE,
            message="DART 서버 응답이 지연되고 있습니다 (타임아웃). 키 자체는 문제가 아닐 수 있습니다.",
        )
    except httpx.ConnectError:
        return DartApiDiagnosis(
            status="network_unreachable",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_NETWORK_UNREACHABLE,
            message="DART 서버에 연결할 수 없습니다. 인터넷 연결을 확인하세요.",
        )
    except httpx.HTTPError as e:
        return DartApiDiagnosis(
            status="network_unreachable",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_NETWORK_UNREACHABLE,
            message=f"네트워크 오류: {type(e).__name__}",
        )

    if code in SUCCESS_CODES:
        return DartApiDiagnosis(status="valid", storage=storage, key_tail_masked=base.key_tail_masked)

    if code in RATE_LIMIT_CODES:
        return DartApiDiagnosis(
            status="rate_limited",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_API_RATE_LIMITED,
            message=f"DART 요청 제한에 도달했습니다 (응답 {code}). 키는 정상일 수 있습니다 — 잠시 후 다시 시도하세요.",
        )

    if code in SERVICE_ISSUE_CODES:
        return DartApiDiagnosis(
            status="network_unreachable",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_NETWORK_UNREACHABLE,
            message=f"DART 서비스 자체 문제로 보입니다 (응답 {code}: {data.get('message', '')}). 키 문제가 아닙니다.",
        )

    return DartApiDiagnosis(
        status="invalid",
        storage=storage,
        key_tail_masked=base.key_tail_masked,
        error_code=DART_API_KEY_INVALID,
        message=f"DART가 키를 거부했습니다 (응답 {code}: {data.get('message', '알 수 없는 오류')}).",
    )


# ---------------------------------------------------------------------------
# DartLens 라이선스 진단 (완전 오프라인 — Ed25519 로컬 검증)
# ---------------------------------------------------------------------------


def diagnose_license() -> LicenseDiagnosis:
    key = licensing.stored_key()
    if not key:
        return LicenseDiagnosis(
            status="missing",
            error_code=DARTLENS_LICENSE_MISSING,
            message=(
                "DartLens 라이선스 키가 없습니다. 구매 후 이메일로 받은 라이선스 키로 "
                "`dartlens-activate <라이선스-키>` 를 실행하세요."
            ),
        )

    res = licensing.verify_key(key)
    if res["valid"]:
        return LicenseDiagnosis(status="active", license_id_masked=licensing.mask_tail(res["license_id"].upper()))

    message = f"라이선스 키가 유효하지 않습니다 — {res['reason']}."
    if licensing.looks_like_dart_api_key(key):
        message += " " + licensing.CROSS_HINT_API_KEY_IN_LICENSE_FIELD
    return LicenseDiagnosis(status="invalid", error_code=DARTLENS_LICENSE_INVALID, message=message)


# ---------------------------------------------------------------------------
# 업데이트 확인 (dartlens_status MCP 도구용)
# ---------------------------------------------------------------------------


@cached(ttl_seconds=3600)
async def fetch_latest_pypi_version() -> str | None:
    """PyPI에서 dartlens-mcp 최신 버전 조회. 실패해도 예외를 던지지 않고 None."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://pypi.org/pypi/dartlens-mcp/json")
        resp.raise_for_status()
        return resp.json().get("info", {}).get("version")
    except Exception:
        return None
