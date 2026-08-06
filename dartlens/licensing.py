"""라이선스 검증 — Ed25519 전자서명 기반, 서버 없이 로컬 검증.

판매자가 개인키로 서명해 발급한 라이선스 키를, 패키지에 박힌 공개키로 검증한다.
공개키는 '검증'만 가능하므로 코드에 노출돼도 새 키를 위조할 수 없다. 유효키
목록도, 인증 서버도 필요 없다(완전 오프라인).

활성화:  dartlens-activate <라이선스-키>
저장 위치:  ~/.dartlens/license.key  (또는 DARTLENS_HOME)
개발 우회:  환경변수 DARTLENS_LICENSE_KEY
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

from dartlens._error_codes import DARTLENS_LICENSE_INVALID, DARTLENS_LICENSE_MISSING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# 제품 태그(4글자). 같은 판매자 키쌍이라도 태그가 달라 StockLens 키는 여기서 거부됨.
PRODUCT = b"DART"

# 판매자 공개키(raw 32B, base64). 검증 전용. 개인키는 판매자 PC에만 존재한다.
_PUBLIC_KEY_B64 = "hHyMqV47+capkk0UTwy9C5dP85RN7KhL1txJ25aZkqw="

_ENV_KEY = "DARTLENS_LICENSE_KEY"

# 구매(상품) 페이지 링크 — 확정되면 이 한 줄만 채우면 모든 안내에 자동 노출된다.
# 예: PURCHASE_URL = "https://litt.ly/dartlens"
PURCHASE_URL = "https://litt.ly/leetkey_lab/sale/hzGHnRY"


def _purchase_line(prefix: str = "· 구매: ") -> str:
    """PURCHASE_URL이 설정돼 있을 때만 안내 줄을 반환(없으면 빈 문자열)."""
    return f"\n{prefix}{PURCHASE_URL}" if PURCHASE_URL else ""


LOCKED_MESSAGE = (
    "🔒 DartLens는 유료 라이선스가 필요합니다.\n"
    "\n"
    "구매 시 발송된 라이선스 키로 활성화하세요:\n"
    "    dartlens-activate <라이선스-키>\n"
    "\n"
    "· 키는 결제 완료 후 이메일로 발송됩니다."
    + _purchase_line()
)

_licensed_cache = False  # 한 번 유효하면 프로세스 동안 재검증 생략


def _home() -> Path:
    base = os.environ.get("DARTLENS_HOME")
    return Path(base) if base else (Path.home() / ".dartlens")


def _license_path() -> Path:
    return _home() / "license.key"


def _decode(key_str: str) -> bytes:
    s = key_str.strip().upper().replace("-", "").replace(" ", "")
    s += "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s)


# ---------------------------------------------------------------------------
# 키 형태 판별 — DART API 키 ↔ DartLens 라이선스 키 혼동 감지 (교차 안내용)
#
# DART API 키는 40자리 hex 문자열이라 base32 알파벳(A-Z, 2-7)에 0/1/8/9가
# 섞여있으면 대개 디코드부터 실패한다 — 두 형식은 실질적으로 겹치지 않는다.
# ---------------------------------------------------------------------------

_DART_API_KEY_RE = re.compile(r"^[0-9a-f]{40}$")


def looks_like_dart_api_key(s: str) -> bool:
    """DART OpenAPI 키(40자리 hex) 형태인지."""
    return bool(_DART_API_KEY_RE.fullmatch((s or "").strip().lower()))


def looks_like_license_shape(s: str) -> bool:
    """상품 라이선스 키(디코드 시 74바이트: 4B 제품태그 + 6B payload + 64B 서명) 형태인지.

    제품 태그/서명 유효성은 안 본다 — '이 값은 우리 라이선스 체계의 키처럼 생겼다'는
    형태 판정만으로 교차 안내(다른 필드에 넣은 게 아닌지)를 붙이기에 충분하다.
    """
    try:
        raw = _decode(s)
    except Exception:
        return False
    return len(raw) == 74


def mask_tail(value: str, keep: int = 4) -> str:
    """마지막 keep자만 남기고 나머지는 '*'로 가림. 로그·JSON에 원문 노출 방지용."""
    v = (value or "").strip()
    if len(v) <= keep:
        return "*" * len(v)
    return "*" * 4 + v[-keep:]


CROSS_HINT_API_KEY_IN_LICENSE_FIELD = (
    "이 값은 DART API 키 형식입니다. API 키를 라이선스 입력란에 넣지 마세요 — "
    "API 키는 `dartlens-setup` 으로 등록하고, 라이선스 키는 `dartlens-activate` 로 등록하세요."
)

CROSS_HINT_LICENSE_IN_API_KEY_FIELD = (
    "이 값은 DartLens 라이선스 키 형식입니다. 라이선스 키를 API 키 입력란에 넣지 마세요 — "
    "라이선스는 `dartlens-activate` 로 등록하고, DART API 키는 `dartlens-setup` 으로 등록하세요."
)


def verify_key(key_str: str) -> dict:
    """키 문자열이 '판매자가 서명한 이 제품의 진짜 키'인지 검증."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(_PUBLIC_KEY_B64))
    except Exception:
        return {"valid": False, "reason": "공개키 설정 오류"}
    try:
        raw = _decode(key_str)
    except Exception:
        return {"valid": False, "reason": "형식 오류(깨진 키)"}
    if len(raw) != 74 or raw[:4] != PRODUCT:
        return {"valid": False, "reason": "이 제품의 키가 아님"}
    payload, sig = raw[:10], raw[10:]
    try:
        pub.verify(sig, payload)
    except InvalidSignature:
        return {"valid": False, "reason": "서명 불일치(위조/변조)"}
    return {"valid": True, "license_id": payload[4:].hex()}


def stored_key() -> str | None:
    env = os.environ.get(_ENV_KEY)
    if env and env.strip():
        return env.strip()
    p = _license_path()
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError):
            # 손상/바이너리 파일(디스크 오류, 강제종료 중 쓰기 등)도 "저장된 키 없음"과
            # 동일하게 취급 — 여기서 예외가 새면 doctor의 라이선스 체크가 통째로 죽는다.
            return None
    return None


def is_licensed() -> bool:
    global _licensed_cache
    if _licensed_cache:
        return True
    k = stored_key()
    if k and verify_key(k)["valid"]:
        _licensed_cache = True
        return True
    return False


def save_key(key_str: str) -> dict:
    res = verify_key(key_str)
    if not res["valid"]:
        return res
    p = _license_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key_str.strip(), encoding="utf-8")
    global _licensed_cache
    _licensed_cache = True
    return res


def _usage() -> str:
    lines = [
        "DartLens 라이선스 활성화",
        "",
        "사용법:",
        "    dartlens-activate <라이선스-키>",
        "",
        "· 키는 결제 완료 후 이메일로 발송됩니다.",
    ]
    if PURCHASE_URL:
        lines.append(f"· 구매: {PURCHASE_URL}")
    return "\n".join(lines)


def _prompt_key() -> str | None:
    """인자 없이 실행하면 터미널에서 키를 직접 붙여넣도록 안내.

    파이프/비대화형 환경(tty 아님)에서는 멈추지 않도록 None을 반환한다.
    """
    if not sys.stdin.isatty():
        return None
    print("DartLens 라이선스 활성화")
    print("결제 후 이메일로 받은 라이선스 키를 붙여넣으세요.")
    if PURCHASE_URL:
        print(f"아직 구매 전이라면 → {PURCHASE_URL}")
    try:
        return input("라이선스 키 ▸ ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def activate_cli() -> None:
    """`dartlens-activate <KEY>` 진입점. 인자가 없으면 키 입력을 안내한다.

    `--stdin`: 키를 stdin에서 읽음 (Manager 등 비대화형 — argv/프로세스 목록 노출 방지).
    `--json`: 사람용 출력 대신 JSON 결과 한 줄 (`license_activated` 키. `api_key_saved`와 혼용 안 함).
    """
    # 파이프/리다이렉트로 stdout이 콘솔이 아니게 되면 Windows는 로캘 코드페이지(cp949 등)로
    # 떨어져 em-dash 같은 문자에서 UnicodeEncodeError가 난다. Manager가 --json 출력을
    # 파이프로 읽는 게 기본 사용 패턴이라 이 reconfigure가 없으면 JSON 계약이 깨진다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    argv = sys.argv[1:]
    json_mode = "--json" in argv
    stdin_mode = "--stdin" in argv
    positional = [a for a in argv if a not in ("--json", "--stdin")]

    if stdin_mode:
        key = sys.stdin.read().strip()
    elif positional:
        key = " ".join(positional).strip()
    else:
        key = _prompt_key()

    if not key:
        licensed = is_licensed()
        if json_mode:
            result = (
                {"license_activated": True}
                if licensed
                else {"license_activated": False, "error_code": DARTLENS_LICENSE_MISSING, "message": "라이선스 키가 입력되지 않았습니다."}
            )
            print(json.dumps(result, ensure_ascii=False))
            sys.exit(0 if licensed else 1)
        if licensed:
            print("현재 상태: 활성화됨 ✅")
            sys.exit(0)
        print("현재 상태: 미활성화 ❌\n")
        print(_usage())
        sys.exit(1)

    res = save_key(key)
    if res["valid"]:
        if json_mode:
            print(json.dumps(
                {"license_activated": True, "license_id_masked": mask_tail(res["license_id"].upper())},
                ensure_ascii=False,
            ))
            sys.exit(0)
        print(f"활성화 완료 ✅  (license_id: {res['license_id']})")
        print("Claude Desktop을 완전히 종료했다가 다시 켜면 DartLens 도구를 쓸 수 있습니다.")
        sys.exit(0)

    cross_hint = CROSS_HINT_API_KEY_IN_LICENSE_FIELD if looks_like_dart_api_key(key) else None
    if json_mode:
        message = res["reason"] + (f" {cross_hint}" if cross_hint else "")
        print(json.dumps(
            {"license_activated": False, "error_code": DARTLENS_LICENSE_INVALID, "message": message},
            ensure_ascii=False,
        ))
        sys.exit(1)
    print(f"활성화 실패 ❌  — {res['reason']}\n")
    print("· 결제 후 발송된 키를 공백 없이 정확히 붙여넣었는지 확인하세요.")
    if cross_hint:
        print(f"· {cross_hint}")
    if PURCHASE_URL:
        print(f"· 키 재발송·문의: {PURCHASE_URL}")
    sys.exit(1)
