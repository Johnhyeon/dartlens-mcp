"""OS 키체인(Windows Credential Manager / macOS Keychain / Secret Service)을 통한
DART API 키 보관.

목적:
- claude_desktop_config.json에 키를 평문으로 박지 않기 위함.
- Windows DPAPI / macOS Keychain은 사용자 계정 단위로 자동 암호화하므로
  config 파일이 유출되어도 키는 노출되지 않는다.

헤드리스 환경(SSH, WSL, Docker, RaspberryPi headless)에는:
- keyring 백엔드가 아예 없거나 (fail backend)
- SecretService 백엔드는 있지만 default collection 이 없어서 D-Bus 프롬프트로
  collection 생성을 요청하다 무한 대기

두 경우 모두 KeyringUnavailableError 를 발생시켜, 호출자가 fallback(평문 모드)을
제공할 수 있게 한다. SecretService hang 은 POSIX SIGALRM 타임아웃으로 차단.
"""

from __future__ import annotations

import os
import signal
import sys

# keyring은 startup-cost가 있을 수 있으므로 사용 시점에 import
SERVICE_NAME = "dartlens"
USERNAME = "DART_API_KEY"

# 과거 SERVICE_NAME으로 저장된 키는 load() 시 fallback 으로 읽고
# delete() 시 함께 정리해서 사용자 재설정 부담을 0으로 만든다.
_LEGACY_SERVICE_NAMES: list[str] = ["dart-mcp-server"]

# SecretService 의 default collection 미존재 시 D-Bus 프롬프트가 무한 대기.
# 헤드리스 환경에선 응답할 GUI 가 없으므로 N초 후 강제 중단.
_KEYRING_OP_TIMEOUT_SEC = 5


class KeyringUnavailableError(RuntimeError):
    """OS keyring 백엔드가 없거나 접근 불가 (헤드리스 환경 등)."""


def _is_headless_linux() -> bool:
    """Linux + GUI 세션 부재 (DISPLAY/WAYLAND_DISPLAY 미설정) → SecretService 잠금 가능성 큼."""
    if sys.platform != "linux":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class _PosixTimeout:
    """POSIX SIGALRM 기반 타임아웃 컨텍스트. Windows 에서는 no-op."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self._prev_handler = None

    def __enter__(self):
        if sys.platform != "win32":
            self._prev_handler = signal.signal(signal.SIGALRM, self._timeout_handler)
            signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        if sys.platform != "win32":
            signal.alarm(0)
            if self._prev_handler is not None:
                signal.signal(signal.SIGALRM, self._prev_handler)
        return False

    @staticmethod
    def _timeout_handler(signum, frame):
        raise TimeoutError(f"keyring operation timed out (>{_KEYRING_OP_TIMEOUT_SEC}s)")


def _get_backend():
    """사용 가능한 keyring 모듈을 반환. 백엔드가 없거나 fail 백엔드면 예외."""
    try:
        import keyring
        from keyring.errors import NoKeyringError
        from keyring.backends import fail as fail_backend
    except ImportError as e:
        raise KeyringUnavailableError(
            f"keyring 라이브러리를 import할 수 없습니다: {e}"
        ) from e

    try:
        backend = keyring.get_keyring()
    except Exception as e:
        raise KeyringUnavailableError(f"keyring 백엔드 조회 실패: {e}") from e

    # fail.Keyring은 "no usable backend" 의미
    if isinstance(backend, fail_backend.Keyring):
        raise KeyringUnavailableError(
            "이 환경에서는 OS 키체인을 사용할 수 없습니다 "
            "(헤드리스/원격 세션 가능성). "
            "claude_desktop_config.json의 env에 DART_API_KEY를 직접 두려면 "
            "'dartlens-setup --plaintext <KEY>' 를 사용하세요."
        )

    return keyring


def save(api_key: str) -> str:
    """키체인에 키 저장. 사용된 backend 이름 반환.

    SIGALRM 타임아웃으로 D-Bus 프롬프트 무한 대기 차단.
    예외는 모두 KeyringUnavailableError 로 변환 — 호출자가 평문 모드 fallback 결정.
    """
    keyring_mod = _get_backend()
    backend_name_str = type(keyring_mod.get_keyring()).__name__

    try:
        with _PosixTimeout(_KEYRING_OP_TIMEOUT_SEC):
            keyring_mod.set_password(SERVICE_NAME, USERNAME, api_key)
    except TimeoutError as e:
        raise KeyringUnavailableError(
            f"{e} — Secret Service 가 응답하지 않습니다 (collection 미존재/D-Bus 프롬프트 hang).\n"
            "  평문 모드로 저장하려면:\n"
            "    dartlens-setup --plaintext <KEY>"
        ) from e
    except Exception as e:
        # ItemNotFoundException, jeepney/dbus 오류, PermissionDenied 등 모두 흡수
        raise KeyringUnavailableError(
            f"키체인 저장 실패: {type(e).__name__}: {e}\n"
            "  평문 모드로 저장하려면:\n"
            "    dartlens-setup --plaintext <KEY>"
        ) from e

    return backend_name_str


def load() -> str | None:
    """키체인에서 키 조회. 없거나 백엔드 부재/응답 없음이면 None.

    현재 SERVICE_NAME에서 못 찾으면 _LEGACY_SERVICE_NAMES 도 순차 확인.
    SecretService hang 방지를 위해 각 호출에 timeout.
    """
    try:
        keyring_mod = _get_backend()
    except KeyringUnavailableError:
        return None

    for service in (SERVICE_NAME, *_LEGACY_SERVICE_NAMES):
        value = None
        try:
            with _PosixTimeout(_KEYRING_OP_TIMEOUT_SEC):
                value = keyring_mod.get_password(service, USERNAME)
        except (TimeoutError, Exception):
            value = None
        if value:
            return value
    return None


def delete() -> bool:
    """키체인에서 키 삭제 (legacy 항목 포함). 하나라도 지웠으면 True."""
    try:
        keyring_mod = _get_backend()
    except KeyringUnavailableError:
        return False

    deleted_any = False
    for service in (SERVICE_NAME, *_LEGACY_SERVICE_NAMES):
        try:
            with _PosixTimeout(_KEYRING_OP_TIMEOUT_SEC):
                keyring_mod.delete_password(service, USERNAME)
            deleted_any = True
        except (TimeoutError, Exception):
            pass
    return deleted_any


def backend_name() -> str:
    """현재 활성 backend의 사람이 읽을 수 있는 이름. 사용 불가면 'unavailable'."""
    try:
        import keyring
        return type(keyring.get_keyring()).__name__
    except Exception:
        return "unavailable"


def backend_status() -> tuple[bool, str]:
    """백엔드 자체의 가용성. (True, backend명) 또는 (False, 사유).

    load()는 백엔드 부재/응답없음을 모두 삼켜 None을 반환하므로 "키를 등록한 적
    없음"과 "키체인이 고장나서 못 읽음"을 구분할 수 없다. diagnostics 모듈이 이
    함수로 후자(DART_API_KEY_STORAGE_FAILED)를 전자(DART_API_KEY_MISSING)와
    구분한다.
    """
    try:
        backend = _get_backend()
        return True, type(backend.get_keyring()).__name__
    except KeyringUnavailableError as e:
        return False, str(e)


def is_responsive(timeout_sec: int = 2) -> bool:
    """짧은 타임아웃으로 keyring 의 실제 응답성을 검증.

    DISPLAY/WAYLAND env 만 보는 휴리스틱은 RasPi OS Desktop 처럼 DISPLAY 가
    설정돼있지만 실제 SecretService 는 잠겨있는 케이스를 못 잡아낸다.
    이 함수는 실제 backend 호출을 시도해서 판정한다.

    True 면 keyring 사용 가능.
    False 면 backend 부재/응답 없음/잠김 등 어떤 이유로든 사용 불가.
    """
    try:
        keyring_mod = _get_backend()
    except KeyringUnavailableError:
        return False

    # 한 번이라도 set_password 가 hang 한 적 있는 backend 면 헤드리스 가능성 매우 큼.
    # get_password 는 일반적으로 set_password 보다 빠르게 실패하지만, SecretService 의
    # 경우 동일한 collection 잠금 흐름을 타므로 같은 hang 위험. 그래서 timeout 사용.
    try:
        with _PosixTimeout(timeout_sec):
            keyring_mod.get_password(SERVICE_NAME, USERNAME)
        return True
    except TimeoutError:
        return False
    except Exception:
        # ItemNotFoundException 등은 "응답은 있다" 의 의미일 수 있지만 대부분
        # 환경에서 keyring 이 정상 동작하면 None 을 그냥 반환한다. 예외가 났다면
        # 안전하게 사용 불가로 판단.
        return False
