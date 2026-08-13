"""MCP 도구 예외를 사용자 친화적 메시지로 변환하는 데코레이터."""

from __future__ import annotations

import functools
import os
import re
import sys

import httpx

from dartlens.licensing import is_licensed, locked_message

_QUERY_RE = re.compile(r"\?[^\s'\"]*")
_CAUSE_MAX = 160


def _cause_text(exc: Exception) -> str:
    """예외 메시지를 사용자에게 보여줄 수 있는 형태로. 없으면 예외 타입 이름.

    쿼리스트링은 통째로 지운다 — httpx 예외 문자열에는 요청 URL이 그대로 들어가고,
    DART 호출 URL에는 `?crtfc_key=<40자리>` 가 실린다. 이 문자열은 화면에 뜨고
    로그에도 남고 지원 번들로 밖에 나간다.
    """
    msg = str(exc).strip()
    if not msg:
        return type(exc).__name__
    msg = _QUERY_RE.sub("?…", msg)
    if len(msg) > _CAUSE_MAX:
        msg = msg[:_CAUSE_MAX] + "…"
    return f"{type(exc).__name__}: {msg}"


def _support_hint() -> str:
    """safe_tool 의 '예상 못한 오류' 버킷에서만 붙이는 자가진단 안내.

    이미 원인이 명확한 예외(라이선스/API키/타임아웃/연결오류 등)엔 안 붙인다 — 사소한
    것까지 문의로 유도하면 노이즈만 늘어난다. sys.platform 은 이 프로세스가 실제 도는
    OS라 Mac/Win 명령을 헷갈릴 일 없이 바로 골라 보여줄 수 있다.
    """
    if sys.platform == "darwin":
        log_cmd = "cd ~/Library/Logs/Claude && zip -r ~/Desktop/claude_logs.zip . && open ~/Desktop"
    elif sys.platform == "win32":
        log_cmd = (
            'powershell -c "Compress-Archive $env:APPDATA\\Claude\\logs\\* '
            '$env:USERPROFILE\\Desktop\\claude_logs.zip -Force; explorer $env:USERPROFILE\\Desktop"'
        )
    else:
        log_cmd = None
    hint = "\n\n계속되면:\n1) Claude 완전 종료 후 재시작 → 다시 시도"
    if log_cmd:
        hint += (
            "\n2) 그래도 안 되면 아래 명령으로 로그를 모아서 osy980315@gmail.com 으로 "
            f"보내주세요\n   {log_cmd}"
        )
    return hint


class DartApiError(Exception):
    """DART API가 비정상 status를 반환했을 때."""

    def __init__(self, status: str, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class MissingApiKeyError(Exception):
    """DART_API_KEY 환경변수가 없을 때."""


def require_api_key() -> str:
    """DART_API_KEY를 반환하거나 MissingApiKeyError를 발생.

    조회 우선순위:
    1. 환경변수 DART_API_KEY (테스트/일시 override 용)
    2. OS 키체인(Windows DPAPI / macOS Keychain / Secret Service)에 저장된 키
    """
    key = os.environ.get("DART_API_KEY", "").strip()
    if key:
        return key

    # 지연 import — keyring 모듈은 첫 사용 시점에만 로드
    from dartlens._keyring import load as _load_from_keyring

    stored = (_load_from_keyring() or "").strip()
    if stored:
        return stored

    raise MissingApiKeyError(
        "DART API 키가 필요합니다.\n"
        "터미널에서 `dartlens-setup`을 실행해 키를 등록하세요.\n"
        "키가 없다면 https://opendart.fss.or.kr 에서 무료 발급 가능."
    )


def safe_tool(func):
    """MCP 도구의 예외를 사용자 친화적 문자열로 변환.

    아울러 라이선스 게이트를 적용한다 — 모든 도구가 이 래퍼를 거치므로,
    활성화되지 않은 경우 데이터 조회 없이 안내 메시지를 반환한다.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not is_licensed():
            return locked_message()
        try:
            return await func(*args, **kwargs)
        except MissingApiKeyError as e:
            return f"⚠️ {e}"
        except DartApiError as e:
            return f"⚠️ DART API 오류 [{e.status}]: {e.message}"
        except httpx.TimeoutException:
            return "⚠️ DART 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요."
        except httpx.ConnectError as e:
            # 원인을 같이 적는다. 예전엔 이 한 줄로 뭉갰는데, 실제 문의(2026-08-13,
            # 뉴질랜드 사용자)에서 연결 실패가 100% 재현되는데도 원인을 좁힐 수가
            # 없었다 — 화면에도 로그에도 "ConnectError" 뿐이라 이름 조회 실패인지·
            # 거부당한 건지·프록시인지 구분이 안 됐다. 그 셋은 사용자가 할 일이 다르다.
            return (
                "⚠️ DART에 연결할 수 없습니다. 인터넷 연결을 확인해주세요.\n"
                f"(원인: {_cause_text(e)})"
            )
        except httpx.HTTPError as e:
            return f"⚠️ 네트워크 오류: {type(e).__name__}\n(원인: {_cause_text(e)})"
        except ValueError as e:
            return f"⚠️ 입력값 오류: {e}"
        except Exception as e:
            return (
                f"⚠️ 처리 중 오류: {_cause_text(e)}\n"
                f"입력값(종목코드/corp_code/날짜)을 다시 확인해주세요."
                + _support_hint()
            )

    return wrapper
