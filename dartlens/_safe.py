"""MCP 도구 예외를 사용자 친화적 메시지로 변환하는 데코레이터."""

from __future__ import annotations

import functools
import os
import re

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


# 원인을 모르는 오류의 안내. 예전엔 "입력값(종목코드/corp_code/날짜)을 다시 확인해주세요"
# 라고 적었는데, 이 분기는 정의상 원인을 모르는 곳이다 — 입력이 틀렸으면 ValueError
# 분기에서 이미 걸렀다. 고객 탓으로 돌리면 멀쩡한 질문을 고치느라 시간을 쓴다.
#
# 문의 길은 LeetKit Manager [지원 문의] 하나다. 예전엔 OS별 zip 명령으로 로그를 직접
# 압축해 메일로 보내라고 했는데, 그렇게 온 문의(2026-09-11)에는 Manager 번들이 넣어주는
# 3-Lens 온라인 진단 요약·최근 호출 실패 집계·키 마스킹이 통째로 빠져 있었다.
_UNKNOWN_ERROR_MESSAGE = (
    "조회 중 문제가 생겼어요. 같은 질문을 한 번 더 해보고, "
    "그래도 같으면 LeetKit Manager 상단 [지원 문의]를 눌러주세요."
)


class DartApiError(Exception):
    """DART API가 비정상 status를 반환했을 때."""

    def __init__(self, status: str, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}" if status else message)


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

    # Claude 답변 안에 그대로 나간다. 키 넣는 곳은 Manager [활성화] 창 하나다
    # (거기서 DART 인증키 발급 페이지도 연다).
    raise MissingApiKeyError(
        "DART 인증키가 아직 없어요. "
        "LeetKit Manager의 DartLens 카드에서 [활성화]를 눌러 DART 인증키를 넣어주세요."
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
            # status 가 없는 오류는 DART 가 코드를 안 실어 보낸 응답이다(점검 페이지 등).
            if not e.status:
                return f"⚠️ DART API 오류: {e.message}"
            return f"⚠️ DART API 오류 [{e.status}]: {e.message}"
        except httpx.TimeoutException as e:
            # 0.6.16에서 ConnectError/HTTPError에는 원인을 붙였는데 이 분기만 빠뜨렸다.
            # 하필 여기가 corpCode.xml(3.4MB) 다운로드가 걸리는 자리였고, 화면에는
            # "지연되고 있습니다"만 떠서 무엇이 얼마나 걸리다 잘렸는지 알 수 없었다.
            return (
                "⚠️ DART 응답이 지연되고 있습니다. 잠시 후 다시 시도해주세요.\n"
                f"(원인: {_cause_text(e)})"
            )
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
            return f"⚠️ {_UNKNOWN_ERROR_MESSAGE}\n(원인: {_cause_text(e)})"

    return wrapper
