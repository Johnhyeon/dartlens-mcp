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
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from dartlens import _error_class
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

LENS = "DartLens"


# ---------------------------------------------------------------------------
# 고객 문구 — 어디에 보이느냐에 따라 앞말만 다르다
#
# 같은 진단 결과가 두 곳에 나간다: Manager 진단 화면(doctor --json 의 summary/action)과
# Claude 답변 안(dartlens_status). 할 일은 같고 가리키는 말만 다르다 — Manager 안에서는
# "DartLens 카드의 [활성화]"로 충분하지만, Claude 답변에서는 어느 프로그램의 카드인지부터
# 말해야 한다. 문구를 두 벌로 따로 쓰면 한쪽만 고쳐지는 일이 생기므로, Manager 문구를
# 기준으로 두고 답변용은 여기서 바꿔 만든다. 터미널 명령은 어느 쪽에도 쓰지 않는다 —
# 주 고객층은 터미널에서 막힌다(Manager 한 길 원칙).
# ---------------------------------------------------------------------------


def answer_copy(text: str | None) -> str | None:
    """Manager 화면 문구를 Claude 답변 안에서 읽히는 앞말로 바꾼다."""
    if not text:
        return text
    out = text.replace(f"{LENS} 카드의 [활성화]에서", f"LeetKit Manager의 {LENS} 카드에서 [활성화]를 눌러")
    out = out.replace(f"{LENS} 카드의 [", f"LeetKit Manager의 {LENS} 카드에서 [")
    out = out.replace("상단 [", "LeetKit Manager 상단 [")
    # [진단]은 Manager 안에서만 누를 수 있다. 답변을 읽는 사람에게는 Claude에게 다시
    # 묻는 것이 같은 확인이다.
    out = out.replace("[진단]을 다시 눌러주세요", "다시 물어봐 주세요")
    return out


# 진단 details.lines 는 Manager 상세 창에 그대로 뜬다. 그래서 한 줄을 "한국어 상황"으로
# 시작하고, 지원할 때 필요한 원문은 줄 끝 괄호 안에 짧게만 붙인다. 원문에서 주소·경로·
# 쿼리스트링·키처럼 생긴 것은 지운다 — 고객 화면이고, [결과 복사]로 밖에도 나간다.
_CATEGORY_LABEL = {
    "tls": "보안 인증서 확인 실패",
    "timeout": "연결 시간 초과",
    "dns": "인터넷 주소 찾기 실패",
    "connect": "서버 연결 실패",
    "blocked": "요청 차단",
    "auth": "인증 거부",
    "schema": "응답 형식 이상",
    "other": "기타 오류",
}
_DETAIL_RAW_MAX = 80
_URL_RE = re.compile(r"https?://\S+|\?\S*")
_PATH_RE = re.compile(r"[A-Za-z]:\\\S*|(?<![\w.])/(?:[\w.-]+/)+[\w.-]*")
# 키처럼 생긴 긴 덩어리(DART 인증키 40자리 hex, 라이선스 키 base32). 예외 문구의
# 쿼리스트링은 _error_detail 이 이미 지우지만, 키가 경로나 본문에 섞여 올 수도 있다.
_SECRET_CHUNK_RE = re.compile(
    r"\b[0-9A-Fa-f]{32,}\b|\b[A-Z2-7]{24,}\b|\b[A-Z2-7]{4,}(?:-[A-Z2-7]{2,}){3,}\b"
)


def _detail_line(korean: str, raw: str | None = None) -> str:
    """상세 창 한 줄. 원문은 걸러서 80자 안으로 괄호에 붙인다(없거나 다 지워지면 생략)."""
    text = " ".join(str(raw or "").split())
    text = _URL_RE.sub("", text)
    text = _PATH_RE.sub("", text)
    text = _SECRET_CHUNK_RE.sub("***", text)
    text = " ".join(text.split()).strip(" :'\"")
    if not text:
        return korean
    if len(text) > _DETAIL_RAW_MAX:
        text = text[: _DETAIL_RAW_MAX - 1] + "…"
    return f"{korean} ({text})"


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
    # 아래 둘은 to_dict()에 안 나간다(JSON 계약 그대로). category 는 _error_class 분류로
    # 할 일 문구를 고르는 데 쓰고, detail 은 doctor 의 details.lines 한 줄이 된다 —
    # Manager 상세 창에 그대로 보이므로 _detail_line 형식(한국어 먼저, 원문은 괄호)을 쓴다.
    category: str | None = None
    detail: str | None = None

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
    # active | missing | invalid | expired | revoked | clock
    # 뒤의 셋은 키 자체는 진짜인데 지금 못 쓰는 상태다 — 예전엔 전부 "active"로
    # 보고해서, 도구는 잠겼는데 Manager는 "라이선스 활성"이라고 말했다.
    status: str
    license_id_masked: str | None = None
    error_code: str | None = None
    message: str = ""
    # 기간이 있는 키(체험판·구독)만 채워진다.
    expires_on: str | None = None
    # doctor details.lines 한 줄(Manager 상세 창에 보인다). to_dict()에는 안 나간다.
    detail: str | None = None

    def to_dict(self) -> dict:
        d: dict = {"status": self.status}
        if self.expires_on is not None:
            d["expires_on"] = self.expires_on
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
            # 사유 원문 뒷부분에는 헤드리스 환경용 터미널 안내(`--plaintext`)가 붙어 있다.
            # Manager 고객이 할 수 있는 일이 아니라서 첫 문장만 상세 줄에 남긴다.
            first_sentence = str(reason).split("\n")[0].split(". ")[0]
            return DartApiDiagnosis(
                status="storage_failed",
                error_code=DART_API_KEY_STORAGE_FAILED,
                message="이 컴퓨터의 키 저장소를 쓸 수 없어서 DART 인증키를 읽지 못했어요.",
                detail=_detail_line("키 저장소: 사용할 수 없음", first_sentence),
            )
        return DartApiDiagnosis(
            status="missing",
            error_code=DART_API_KEY_MISSING,
            message="DART 인증키가 아직 없어요.",
        )

    if licensing.looks_like_license_shape(key):
        return DartApiDiagnosis(
            status="invalid",
            storage=storage,
            key_tail_masked=licensing.mask_tail(key),
            error_code=DART_API_KEY_INVALID,
            message="DART 인증키 자리에 라이선스 키가 들어가 있어요.",
            category="auth",
        )

    return DartApiDiagnosis(status="valid", storage=storage, key_tail_masked=licensing.mask_tail(key))


def dart_api_action(diag: DartApiDiagnosis, *, in_answer: bool = False) -> str | None:
    """DART 인증키 진단의 할 일 한 줄(Manager 화면 기준, in_answer=True 면 Claude 답변용)."""
    if diag.status == "valid":
        return None
    if diag.status == "missing":
        action = f"{LENS} 카드의 [활성화]를 눌러 DART 인증키를 넣어주세요."
    elif diag.status == "storage_failed":
        action = "상단 [지원 문의]를 눌러주세요."
    elif diag.status == "invalid" and diag.category == "blocked":
        # 012(IP 차단). 인증키를 다시 넣게 하면 멀쩡한 키를 재발급받느라 시간만 쓴다.
        action = "인증키를 다시 넣지 말고 상단 [지원 문의]를 눌러주세요."
    elif diag.category:
        action = _error_class.action_for(diag.category, LENS)
    else:
        # DART 서비스 점검(800/900) — 인증키도 연결도 아닌, 기다리면 풀리는 일이다.
        action = "잠시 뒤 [진단]을 다시 눌러주세요. 그래도 같으면 상단 [지원 문의]를 눌러주세요."
    return answer_copy(action) if in_answer else action


# DART company.json — 삼성전자(00126380)는 항상 존재하는 안정적 corp_code라
# "가벼운 엔드포인트 1회 호출"로 키 유효성만 확인하는 용도에 적합하다.
_VALIDATE_URL = "https://opendart.fss.or.kr/api/company.json"
_VALIDATE_CORP_CODE = "00126380"

RATE_LIMIT_CODES = {"020", "021"}
# DART 서비스 자체 문제 — 키 불량으로 오판하면 안 됨. "901"(개인정보 보유기간 만료)은
# 계정 자체 문제라 여기 넣지 않는다 — SUCCESS/RATE_LIMIT 어디에도 안 걸리면 기본적으로
# invalid 분기로 떨어져 DART 원문 메시지("사용자 계정의 개인정보 보유기간이 만료되었습니다")가
# 그대로 사용자에게 전달된다. "서비스 문제라 당신 잘못 아님"이라고 오도하지 않기 위함.
SERVICE_ISSUE_CODES = {"800", "900"}
SUCCESS_CODES = {"000", "013"}  # 013 = 조회 결과 없음. 연결 성공으로 취급.
# 012 = 접근할 수 없는 IP. 키 값 문제가 아니라 접속 위치 문제다.
IP_BLOCKED_CODES = {"012"}


async def check_dart_key_online(api_key: str) -> tuple[str, dict]:
    """DART 가벼운 엔드포인트 1회 호출 → (status_code, raw_json).

    네트워크 예외(httpx.*)는 그대로 전파 — 호출자가 network_unreachable 로 매핑한다.
    DART가 200을 주면서 JSON이 아니거나 예상 형태가 아닌 응답(점검 페이지 등)을 줄 수도
    있는데, 그 경우도 httpx.HTTPError로 통일해서 던진다 — 이미 모든 호출부가
    httpx.HTTPError를 폴백으로 잡고 있으므로 새 예외 타입을 각 호출부에 추가할 필요가 없다.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _VALIDATE_URL,
            params={"crtfc_key": api_key, "corp_code": _VALIDATE_CORP_CODE},
        )
    resp.raise_for_status()
    try:
        data = resp.json()
        status = str(data.get("status", "")).strip()
    except Exception as e:
        raise httpx.HTTPError(f"DART 응답을 해석할 수 없습니다: {type(e).__name__}") from e
    return status, data


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
    except (httpx.HTTPError, OSError) as e:
        # 예전엔 타임아웃·ConnectError·나머지 셋으로만 갈랐다. 그러면 백신이 TLS를
        # 가로챈 PC(2026-08-13 문의)와 인터넷이 끊긴 PC가 같은 "연결할 수 없습니다"를
        # 받는다 — 할 일이 전혀 다른데. 세 Lens 공통 분류로 원인을 나눈다.
        # OSError 도 받는 이유: httpx 를 안 거친 ssl/소켓 오류가 새면 doctor 전체가 죽는다.
        category = _error_class.classify_exception(e)
        if category == "timeout":
            message = "DART 응답이 늦어서 인증키를 확인하지 못했어요."
        elif category == "blocked":
            message = "DART가 요청을 막아서 인증키를 확인하지 못했어요."
        else:
            message = "DART에 연결하지 못해서 인증키를 확인하지 못했어요."
        return DartApiDiagnosis(
            status="network_unreachable",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_NETWORK_UNREACHABLE,
            message=message,
            category=category,
            detail=_detail_line(
                f"DART 연결: 실패 ({_CATEGORY_LABEL.get(category, '기타 오류')})",
                f"{type(e).__name__}: {e}",
            ),
        )

    dart_message = str(data.get("message") or "").strip().rstrip(".")
    reply = f"응답 {code}: {dart_message}" if dart_message else f"응답 {code}"

    if code in SUCCESS_CODES:
        return DartApiDiagnosis(status="valid", storage=storage, key_tail_masked=base.key_tail_masked)

    if code in RATE_LIMIT_CODES:
        return DartApiDiagnosis(
            status="rate_limited",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_API_RATE_LIMITED,
            message=f"DART 요청 한도에 걸렸어요({reply}). 인증키는 정상일 수 있어요.",
            category="blocked",
            detail=_detail_line("DART 연결: 요청 한도 초과", reply),
        )

    if code in SERVICE_ISSUE_CODES:
        return DartApiDiagnosis(
            status="network_unreachable",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_NETWORK_UNREACHABLE,
            message=f"DART 서비스 점검이나 일시 장애로 보여요({reply}). 인증키 문제는 아니에요.",
            detail=_detail_line("DART 연결: 서비스 점검·장애", reply),
        )

    if code in IP_BLOCKED_CODES:
        # 키 값이 틀린 게 아니라 이 PC의 IP가 막힌 것이다. "키를 거부했다"로 적으면
        # 멀쩡한 키를 다시 발급·등록하게 된다. status·error_code 는 LeetKit Manager
        # 계약이라 그대로 두고 문구만 원인대로 적는다.
        if not dart_message:
            reply = f"응답 {code}: 접근할 수 없는 IP입니다"
        return DartApiDiagnosis(
            status="invalid",
            storage=storage,
            key_tail_masked=base.key_tail_masked,
            error_code=DART_API_KEY_INVALID,
            message=f"DART가 이 컴퓨터의 IP 접속을 막았어요({reply}). 인증키가 틀렸다는 뜻은 아니에요.",
            category="blocked",
            detail=_detail_line("DART 연결: IP 접속 차단", reply),
        )

    # 901(개인정보 보유기간 만료) 같은 계정 문제도 여기로 온다. DART 원문을 그대로 실어야
    # 고객이 무엇을 풀어야 하는지 안다.
    if not dart_message:
        reply = f"응답 {code}: 알 수 없는 오류"
    return DartApiDiagnosis(
        status="invalid",
        storage=storage,
        key_tail_masked=base.key_tail_masked,
        error_code=DART_API_KEY_INVALID,
        message=f"DART가 인증키를 거부했어요({reply}).",
        category="auth",
        detail=_detail_line("DART 연결: 인증키 거부", reply),
    )


# ---------------------------------------------------------------------------
# DartLens 라이선스 진단 (완전 오프라인 — Ed25519 로컬 검증)
# ---------------------------------------------------------------------------


# 상태마다 할 일이 서로 다르다 — 셋 다 "키를 다시 넣으세요"가 답이 아니다.
# 문구는 세 Lens 공통 사양(2-2)과 글자까지 맞춘다. 앞은 상황(summary), 뒤는 할 일(action).
_LICENSE_SUMMARY = {
    "missing": "라이선스 키가 아직 없어요.",
    "invalid": "저장된 라이선스 키를 확인할 수 없어요.",
    "expired": "사용 기간이 끝났어요.",
    "revoked": "이 라이선스 키는 사용이 중지돼 있어요.",
    "clock": "이 컴퓨터의 날짜가 실제보다 과거로 되어 있어요.",
}

_LICENSE_ACTION = {
    "missing": f"{LENS} 카드의 [활성화]를 눌러 메일로 받은 키를 넣어주세요.",
    "invalid": f"{LENS} 카드의 [활성화]를 눌러 메일로 받은 키를 다시 넣어주세요.",
    "expired": f"{LENS} 카드의 [구매]를 누르고, 받은 키를 [활성화]로 넣어주세요.",
    "revoked": "착오라면 상단 [지원 문의]를 눌러주세요.",
    "clock": "날짜와 시간을 오늘로 맞춘 뒤 [진단]을 다시 눌러주세요.",
}

# 라이선스 칸에 DART 인증키를 넣은 경우. 흔한 실수라 "확인할 수 없어요"만으로는
# 무엇을 바꿔 넣어야 하는지 모른다.
_LICENSE_HOLDS_API_KEY = "라이선스 키 자리에 DART 인증키가 들어가 있어요."

# 상세 창에 보이는 검증 실패 사유. "서명 불일치(위조/변조)" 원문은 정상 구매자에게
# 의심받는다는 인상을 준다 — 대부분은 복사 실수다.
_LICENSE_REASON_LABEL = {
    licensing._REASON_MALFORMED: "키 모양이 달라요",
    licensing._REASON_WRONG_PRODUCT: "DartLens 키가 아니에요",
    licensing._REASON_BAD_SIGNATURE: "키 내용이 맞지 않아요",
    licensing._REASON_PUBKEY: "확인 설정 오류",
}


def license_action(diag: LicenseDiagnosis, *, in_answer: bool = False) -> str | None:
    """라이선스 진단의 할 일 한 줄(Manager 화면 기준, in_answer=True 면 Claude 답변용)."""
    action = _LICENSE_ACTION.get(diag.status)
    return answer_copy(action) if in_answer else action


def diagnose_license() -> LicenseDiagnosis:
    key = licensing.stored_key()
    if not key:
        return LicenseDiagnosis(
            status="missing",
            error_code=DARTLENS_LICENSE_MISSING,
            message=_LICENSE_SUMMARY["missing"],
        )

    res = licensing.verify_key(key)
    if res["valid"]:
        # 키에 박힌 날짜가 아니라 실제로 끝나는 날. 이 값이 매니저의 "N일 남음"
        # 배지로 나가므로, 서명 날짜를 쓰면 "12월까지"라고 해놓고 9월에 잠긴다.
        expiry = licensing.effective_expiry(res)
        masked = licensing.mask_tail(res["license_id"].upper())
        reason = licensing.license_block_reason()
        if reason in ("expired", "revoked", "clock"):
            return LicenseDiagnosis(
                status=reason,
                license_id_masked=masked,
                error_code=DARTLENS_LICENSE_INVALID,
                message=_LICENSE_SUMMARY[reason],
                expires_on=expiry.isoformat() if expiry else None,
            )
        return LicenseDiagnosis(
            status="active",
            license_id_masked=masked,
            expires_on=expiry.isoformat() if expiry else None,
        )

    message = _LICENSE_SUMMARY["invalid"]
    if licensing.looks_like_dart_api_key(key):
        message += " " + _LICENSE_HOLDS_API_KEY
    return LicenseDiagnosis(
        status="invalid",
        error_code=DARTLENS_LICENSE_INVALID,
        message=message,
        detail=_detail_line(
            "라이선스 키 확인: 실패",
            _LICENSE_REASON_LABEL.get(res.get("reason"), res.get("reason")),
        ),
    )


# ---------------------------------------------------------------------------
# 최근 조회 실패 (RECENT_TOOL_FAILURES) — metrics 기록만 읽는다, 네트워크 없음
#
# 온라인 진단은 "지금 연결되나"만 본다. 고객이 겪은 실패는 이미 도구 호출 기록에
# 남아 있는데, 진단이 그걸 안 읽으면 카드는 "정상"인데 Claude는 계속 실패하는 반대말
# 상태가 된다(지원 번들 요약이 같은 이유로 고쳐졌다).
#
# 한계: 도구가 예외 없이 "⚠️ …" 문자열을 돌려준 실패는 기록에 error 로 안 남아 못 본다.
# ---------------------------------------------------------------------------

RECENT_FAILURE_WINDOW_HOURS = 48
_RECENT_LINES_MAX = 8
_RECENT_DETAIL_PREVIEW = 120


@dataclass
class RecentFailuresDiagnosis:
    status: str  # ok | warn (기록을 못 읽으면 doctor 가 info-skip 으로 만든다)
    summary: str
    action: str | None = None
    error_code: str | None = None
    lines: list = field(default_factory=list)


def _mask_secrets(text: str) -> str:
    return _SECRET_CHUNK_RE.sub("***", text)


def _hhmm(timestamp) -> str:
    try:
        return datetime.fromisoformat(str(timestamp)).strftime("%H:%M")
    except Exception:
        return "?"


def diagnose_recent_tool_failures(records: list) -> RecentFailuresDiagnosis:
    """최근 호출 기록(시간순)으로 '아직 풀리지 않은 실패'가 있는지 본다.

    같은 도구가 실패 뒤에 성공했으면 풀린 것으로 본다 — 한 번 삐끗한 기록 때문에
    이틀 내내 카드가 '주의'로 남으면 진단을 믿지 않게 된다. 끝에 남은 실패가 아직
    실패인지는 `_error_class.still_failing`이 정한다(세 Lens 공통). AI 앱이 취소한 호출
    (CancelledError)은 DartLens 고장이 아니라서 실패로 세지 않고, 앞선 상태도 바꾸지
    않는다.
    """
    records = [row for row in records if isinstance(row, dict)]
    total = len(records)
    if not total:
        return RecentFailuresDiagnosis(
            status="ok", summary=f"최근 이틀 동안 AI 앱이 {LENS}를 쓴 기록이 없어요."
        )

    per_tool: dict = {}
    failure_count = 0
    for row in records:
        tool = str(row.get("tool") or "unknown")
        info = per_tool.setdefault(tool, {"failed": [], "cancelled": [], "trailing": []})
        error_type = row.get("error")
        if not error_type:
            info["trailing"] = []
            continue
        category = _error_class.classify_error(str(error_type), row.get("error_detail"))
        if category == "cancelled":
            info["cancelled"].append(row)
            continue
        failure_count += 1
        info["failed"].append((row, category))
        info["trailing"].append(category)

    def _line(tool: str, count_text: str, row: dict, category: str) -> str:
        detail = _mask_secrets(str(row.get("error_detail") or ""))[:_RECENT_DETAIL_PREVIEW]
        error_text = f"{row.get('error')}: {detail}" if detail else str(row.get("error"))
        return f"{tool}: {count_text}, 마지막 {_hhmm(row.get('timestamp'))}, 분류 {category}, {error_text}"

    def _failed_line(tool: str, info: dict) -> str:
        row, category = info["failed"][-1]
        return _line(tool, f"실패 {len(info['failed'])}번", row, category)

    def _cancelled_line(tool: str, info: dict) -> str:
        return _line(tool, f"취소 {len(info['cancelled'])}번(실패로 안 셈)", info["cancelled"][-1], "cancelled")

    def _last_failed_at(item) -> str:
        return str(item[1]["failed"][-1][0].get("timestamp") or "")

    outstanding = sorted(
        ((t, i) for t, i in per_tool.items() if _error_class.still_failing(i["trailing"])),
        key=_last_failed_at,
        reverse=True,
    )
    resolved = sorted(
        ((t, i) for t, i in per_tool.items() if i["failed"] and not _error_class.still_failing(i["trailing"])),
        key=_last_failed_at,
        reverse=True,
    )
    lines = [_failed_line(t, i) for t, i in outstanding + resolved]
    lines += [_cancelled_line(t, i) for t, i in per_tool.items() if i["cancelled"]]
    lines = lines[:_RECENT_LINES_MAX]

    if failure_count == 0:
        return RecentFailuresDiagnosis(
            status="ok", summary=f"최근 이틀 동안 조회 {total}번이 모두 정상이었어요.", lines=lines
        )
    if not outstanding:
        return RecentFailuresDiagnosis(
            status="ok",
            summary=f"최근 이틀 동안 조회 {total}번 중 {failure_count}번이 실패했지만, 계속 실패하고 있지는 않아요.",
            lines=lines,
        )

    # 대표 분류: 남아 있는 실패에서 가장 많은 것, 같으면 더 최근 것.
    tally: dict = {}
    for _tool, info in outstanding:
        row, category = info["failed"][-1]
        count, latest = tally.get(category, (0, ""))
        tally[category] = (count + 1, max(latest, str(row.get("timestamp") or "")))
    main_category = max(tally, key=lambda c: tally[c])
    return RecentFailuresDiagnosis(
        status="warn",
        summary=f"최근 조회 중 아직 실패로 남아 있는 것이 {len(outstanding)}가지 있어요.",
        action=_error_class.action_for(main_category, LENS),
        error_code=f"RECENT_TOOL_FAILURES_{main_category.upper()}",
        lines=lines,
    )


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
