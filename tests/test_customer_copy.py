"""고객에게 보이는 문구 전수 검사 — 터미널 명령·메일 주소·환경변수 이름이 다시 새지 않게.

고객이 문구를 보는 자리는 세 곳이다.
  1. Claude 답변 안: 잠금 안내, dartlens_status, 도구 오류 응답
  2. Manager 진단 화면: doctor --json 의 checks[].summary / action
  3. Manager 활성화 창: activate --json / setup --json 의 실패 사유
주 고객층은 터미널에서 막힌다. 할 일은 LeetKit Manager 버튼으로만 안내하고, 버튼 이름은
지금 화면 글자 그대로여야 한다(세 Lens 공통 사양 1장).

진단 details.lines 는 여기서 보지 않는다 — 지원용 원문이 들어가는 자리다.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import re
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from dartlens import _corp_code, _keyring, _safe, diagnostics, doctor, licensing, server, setup_claude

FORBIDDEN = [
    re.compile(r"\b(stocklens|dartlens|telegramlens)-(activate|doctor|setup|login|broker)\b", re.I),
    re.compile(r"\buv (tool|pip|run)\b", re.I),
    re.compile(r"터미널"),
    re.compile(r"PowerShell", re.I),
    re.compile(r"@gmail\.com", re.I),
    re.compile(r"STOCKLENS_HOME|DARTLENS_HOME|TELEGRAMLENS_HOME", re.I),
]

ALLOWED_BUTTONS = {
    "활성화", "구매", "업데이트", "MCP 등록", "진단", "복구", "텔레그램 로그인", "증권사 연결",
    "지원 문의", "문제 해결",
}


def assert_customer_copy(text, where=""):
    if not text:
        return
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{where}: 금지 패턴 {pattern.pattern!r} → {text!r}"
    for name in re.findall(r"\[([^\]]+)\]", text):
        assert name in ALLOWED_BUTTONS, f"{where}: 없는 버튼 이름 [{name}] → {text!r}"


def _fake_license_shaped_key() -> str:
    return base64.b32encode(os.urandom(74)).decode()


# ---------------------------------------------------------------------------
# 1. 잠금 안내 4종 (Claude 답변 안)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["missing", "invalid", "expired", "revoked", "clock"])
def test_locked_messages(monkeypatch, reason):
    monkeypatch.setattr(licensing, "license_block_reason", lambda: reason)
    text = licensing.locked_message()
    assert_customer_copy(text, f"locked_message({reason})")
    assert "LeetKit Manager" in text


def test_locked_messages_point_to_the_right_button():
    assert "DartLens 카드에서 [활성화]" in licensing.LOCKED_MESSAGE
    assert "[지원 문의]" in licensing.LOCKED_MESSAGE
    assert "[구매]" in licensing.EXPIRED_MESSAGE and "[활성화]" in licensing.EXPIRED_MESSAGE
    assert "[지원 문의]" in licensing.REVOKED_MESSAGE
    assert "[활성화]" not in licensing.REVOKED_MESSAGE  # 키가 있는 사람이다 — 재입력이 답이 아니다
    assert "다시 물어봐 주세요" in licensing.CLOCK_MESSAGE


def test_locked_message_is_the_shared_spec_text():
    assert licensing.LOCKED_MESSAGE == (
        "🔒 DartLens를 쓰려면 라이선스 키가 필요해요.\n"
        "\n"
        "LeetKit Manager의 DartLens 카드에서 [활성화]를 눌러 메일로 받은 키를 넣어주세요.\n"
        "그래도 같으면 LeetKit Manager 상단 [지원 문의]를 눌러주세요."
    )


# ---------------------------------------------------------------------------
# 진단 상태 만들기
# ---------------------------------------------------------------------------


def _license_diag(monkeypatch, state):
    if state == "missing":
        monkeypatch.setattr(licensing, "stored_key", lambda: None)
    elif state == "invalid":
        monkeypatch.setattr(licensing, "stored_key", lambda: "NOT-A-REAL-KEY")
    elif state == "invalid_api_key":
        monkeypatch.setattr(licensing, "stored_key", lambda: "c" * 40)
    else:
        monkeypatch.setattr(licensing, "stored_key", lambda: "whatever")
        monkeypatch.setattr(licensing, "verify_key", lambda k: {"valid": True, "license_id": "abcdef123456"})
        monkeypatch.setattr(licensing, "effective_expiry", lambda res: None)
        monkeypatch.setattr(licensing, "license_block_reason", lambda: None if state == "active" else state)
    return diagnostics.diagnose_license()


def _dart_offline_diag(state):
    if state == "missing":
        with patch.object(diagnostics, "resolve_dart_api_key", return_value=(None, None)), \
             patch.object(diagnostics.keyring_helper, "backend_status", return_value=(True, "Mock")):
            return diagnostics.diagnose_dart_api_key()
    if state == "storage_failed":
        reason = (
            "이 환경에서는 OS 키체인을 사용할 수 없습니다 (헤드리스/원격 세션 가능성). "
            "claude_desktop_config.json의 env에 DART_API_KEY를 직접 두려면 "
            "'dartlens-setup --plaintext <KEY>' 를 사용하세요."
        )
        with patch.object(diagnostics, "resolve_dart_api_key", return_value=(None, None)), \
             patch.object(diagnostics.keyring_helper, "backend_status", return_value=(False, reason)):
            return diagnostics.diagnose_dart_api_key()
    if state == "license_shape":
        with patch.object(diagnostics, "resolve_dart_api_key", return_value=(_fake_license_shaped_key(), "env")):
            return diagnostics.diagnose_dart_api_key()
    raise AssertionError(state)


ONLINE_CASES = {
    "timeout": {"side_effect": httpx.ReadTimeout("The read operation timed out")},
    "tls": {"side_effect": httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")},
    "dns": {"side_effect": httpx.ConnectError("[Errno 11001] getaddrinfo failed")},
    "connect": {"side_effect": httpx.ConnectError("[WinError 10061] Connection refused")},
    "blocked_http": {"side_effect": httpx.HTTPError("Client error '403 Forbidden' for url 'https://opendart.fss.or.kr/api/company.json?crtfc_key=abc'")},
    "other_http": {"side_effect": httpx.HTTPError("DART 응답을 해석할 수 없습니다: ValueError")},
    "os_error": {"side_effect": OSError("unexpected socket failure")},
    "rate_limited": {"return_value": ("020", {"message": "요청 제한을 초과했습니다"})},
    "service": {"return_value": ("800", {"message": "시스템 점검으로 인한 서비스 중단입니다"})},
    "ip_blocked": {"return_value": ("012", {"message": "접근할 수 없는 IP입니다."})},
    "rejected": {"return_value": ("010", {"message": "등록되지 않은 키입니다"})},
    "account": {"return_value": ("901", {"message": "사용자 계정의 개인정보 보유기간이 만료되었습니다"})},
}


def _dart_online_diag(case):
    with patch.dict(os.environ, {"DART_API_KEY": "a" * 40}), \
         patch.object(diagnostics, "check_dart_key_online", AsyncMock(**ONLINE_CASES[case])):
        return asyncio.run(diagnostics.diagnose_dart_api_key_online())


def _cache_diag(state):
    base = {"exists": True, "last_updated": "2026-09-17T09:00:00", "is_fresh": True,
            "entry_count": 100, "parseable": True, "writable": True}
    if state == "missing":
        return {**base, "exists": False, "last_updated": None, "entry_count": None, "parseable": None}
    if state == "corrupt":
        return {**base, "parseable": False, "entry_count": 0}
    if state == "empty":
        return {**base, "entry_count": 0}
    if state == "not_writable":
        return {**base, "writable": False}
    if state == "stale":
        return {**base, "is_fresh": False}
    return base


def _recent_diag(category):
    detail = {
        "tls": ("ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] x"),
        "timeout": ("ReadTimeout", "timed out"),
        "dns": ("ConnectError", "[Errno 11001] getaddrinfo failed"),
        "connect": ("ConnectError", "Connection refused"),
        "blocked": ("DartApiError", "[020] 요청 제한"),
        "auth": ("DartApiError", "[010] 등록되지 않은 키"),
        "schema": ("KeyError", "'list'"),
        "other": ("RuntimeError", "boom"),
    }[category]
    rows = [{"timestamp": "2026-09-17T10:00:00", "tool": "search_company", "error": detail[0], "error_detail": detail[1]}]
    return diagnostics.diagnose_recent_tool_failures(rows)


def _report(monkeypatch, tmp_path, *, license_diag, dart_diag, cache_diag, recent_diag, configs="none"):
    desktop = tmp_path / "claude_desktop_config.json"
    code = tmp_path / ".claude.json"
    codex = tmp_path / "config.toml"
    if configs == "legacy":
        desktop.write_text(json.dumps({"mcpServers": {"dart-mcp": {"command": "x"}}}), encoding="utf-8")
    elif configs == "broken":
        desktop.write_text("{not json", encoding="utf-8")
        code.write_text(json.dumps({"mcpServers": {"dartlens": {"command": str(tmp_path / "gone.exe")}}}), encoding="utf-8")
    elif configs == "relative":
        desktop.write_text(json.dumps({"mcpServers": {"dartlens": {"command": "dartlens-not-on-path"}}}), encoding="utf-8")
        codex.write_text('[mcp_servers.dartlens]\ncommand = "dartlens-not-on-path"\n', encoding="utf-8")

    monkeypatch.setattr(doctor, "get_claude_desktop_config_path", lambda: desktop)
    monkeypatch.setattr(doctor, "get_claude_code_config_path", lambda: code)
    monkeypatch.setattr(doctor, "get_codex_config_path", lambda: codex)
    monkeypatch.setattr(doctor, "_find_uv", lambda: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_uv_tool_bin_dirs", lambda: [])
    monkeypatch.setattr(doctor.sysconfig, "get_paths", lambda *a, **k: {"scripts": str(tmp_path / "no-scripts")})
    monkeypatch.setattr(diagnostics, "diagnose_license", lambda: license_diag)
    monkeypatch.setattr(diagnostics, "diagnose_dart_api_key", lambda **k: dart_diag)
    monkeypatch.setattr(_corp_code, "cache_diagnosis", lambda: cache_diag)
    monkeypatch.setattr(doctor, "_diagnose_recent_failures", lambda: recent_diag)
    state = doctor.run_diagnostics(online=False)
    return doctor.build_report(state, online=False)


def _assert_report_copy(report, label):
    for check in report["checks"]:
        assert_customer_copy(check["summary"], f"{label}/{check['id']}.summary")
        assert_customer_copy(check["action"], f"{label}/{check['id']}.action")


# ---------------------------------------------------------------------------
# 2. 진단 JSON (Manager 화면)
# ---------------------------------------------------------------------------

REPORT_SCENARIOS = [
    # license, dart(offline|online:<case>), cache, recent, configs
    ("missing", "missing", "missing", "tls", "none"),
    ("invalid", "storage_failed", "corrupt", "timeout", "legacy"),
    ("invalid_api_key", "license_shape", "empty", "dns", "broken"),
    ("expired", "online:rate_limited", "not_writable", "connect", "relative"),
    ("revoked", "online:ip_blocked", "stale", "blocked", "none"),
    ("clock", "online:tls", "ok", "auth", "none"),
    ("active", "online:timeout", "ok", "schema", "none"),
    ("active", "online:dns", "ok", "other", "none"),
    ("active", "online:connect", "ok", "tls", "none"),
    ("active", "online:blocked_http", "ok", "tls", "none"),
    ("active", "online:other_http", "ok", "tls", "none"),
    ("active", "online:os_error", "ok", "tls", "none"),
    ("active", "online:service", "ok", "tls", "none"),
    ("active", "online:rejected", "ok", "tls", "none"),
    ("active", "online:account", "ok", "tls", "none"),
]


@pytest.mark.parametrize("lic, dart, cache, recent, configs", REPORT_SCENARIOS)
def test_doctor_json_summary_and_action(monkeypatch, tmp_path, lic, dart, cache, recent, configs):
    license_diag = _license_diag(monkeypatch, lic)
    dart_diag = _dart_online_diag(dart.split(":", 1)[1]) if dart.startswith("online:") else _dart_offline_diag(dart)
    report = _report(
        monkeypatch, tmp_path,
        license_diag=license_diag, dart_diag=dart_diag, cache_diag=_cache_diag(cache),
        recent_diag=_recent_diag(recent), configs=configs,
    )
    _assert_report_copy(report, f"{lic}/{dart}/{cache}/{recent}/{configs}")
    by_id = {c["id"]: c for c in report["checks"]}
    # 문제가 있는 항목은 할 일이 비어 있으면 안 된다.
    for cid in ("LICENSE_ACTIVE", "DART_API_KEY", "CORP_CODE_CACHE", "RECENT_TOOL_FAILURES", "MCP_CONFIG_VALID"):
        if by_id[cid]["status"] in ("warn", "fail"):
            assert by_id[cid]["action"], f"{cid} action 없음"
    assert "API 키 원문" not in json.dumps(report, ensure_ascii=False)
    assert "a" * 40 not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize(
    "state, summary, action",
    [
        ("missing", "라이선스 키가 아직 없어요.", "DartLens 카드의 [활성화]를 눌러 메일로 받은 키를 넣어주세요."),
        ("invalid", "저장된 라이선스 키를 확인할 수 없어요.", "DartLens 카드의 [활성화]를 눌러 메일로 받은 키를 다시 넣어주세요."),
        ("expired", "사용 기간이 끝났어요.", "DartLens 카드의 [구매]를 누르고, 받은 키를 [활성화]로 넣어주세요."),
        ("revoked", "이 라이선스 키는 사용이 중지돼 있어요.", "착오라면 상단 [지원 문의]를 눌러주세요."),
        ("clock", "이 컴퓨터의 날짜가 실제보다 과거로 되어 있어요.", "날짜와 시간을 오늘로 맞춘 뒤 [진단]을 다시 눌러주세요."),
    ],
)
def test_license_check_is_the_shared_spec_text(monkeypatch, tmp_path, state, summary, action):
    report = _report(
        monkeypatch, tmp_path,
        license_diag=_license_diag(monkeypatch, state), dart_diag=_dart_offline_diag("missing"),
        cache_diag=_cache_diag("ok"), recent_diag=_recent_diag("tls"),
    )
    check = next(c for c in report["checks"] if c["id"] == "LICENSE_ACTIVE")
    assert check["summary"] == summary
    assert check["action"] == action


def test_spec_rows_for_mcp_cache_and_dart_key(monkeypatch, tmp_path):
    report = _report(
        monkeypatch, tmp_path,
        license_diag=_license_diag(monkeypatch, "active"), dart_diag=_dart_offline_diag("missing"),
        cache_diag=_cache_diag("not_writable"), recent_diag=_recent_diag("tls"),
    )
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["MCP_CONFIG_VALID"]["summary"] == "DartLens가 아직 AI 앱에 등록되지 않았어요."
    assert by_id["MCP_CONFIG_VALID"]["action"] == "DartLens 카드의 [MCP 등록]을 눌러주세요."
    assert by_id["CORP_CODE_CACHE"]["summary"] == "캐시 폴더에 쓸 수 없어요."
    assert by_id["CORP_CODE_CACHE"]["action"] == "상단 [지원 문의]를 눌러주세요."
    assert by_id["DART_API_KEY"]["summary"] == "DART 인증키가 아직 없어요."
    assert by_id["DART_API_KEY"]["action"] == "DartLens 카드의 [활성화]를 눌러 DART 인증키를 넣어주세요."


@pytest.mark.parametrize(
    "case, expected_phrase",
    [
        ("tls", "백신이나 회사 보안 프로그램"),
        ("timeout", "연결이 느려서"),
        ("dns", "인터넷 주소를 찾지 못했어요"),
        ("connect", "데이터 서버에 연결하지 못했어요"),
        ("blocked_http", "데이터 제공처가 요청을 막았어요"),
        ("rate_limited", "데이터 제공처가 요청을 막았어요"),
        ("rejected", "[활성화]에서 DART 인증키를 다시 넣어주세요"),
        ("ip_blocked", "인증키를 다시 넣지 말고 상단 [지원 문의]"),
    ],
)
def test_online_failures_get_category_action(case, expected_phrase):
    diag = _dart_online_diag(case)
    assert expected_phrase in diagnostics.dart_api_action(diag)


def test_ip_block_still_not_reported_as_key_rejection():
    diag = _dart_online_diag("ip_blocked")
    assert diag.status == "invalid" and diag.error_code == diagnostics.DART_API_KEY_INVALID
    assert "IP" in diag.message and "거부" not in diag.message


def test_online_detail_line_is_korean_first_and_clean():
    diag = _dart_online_diag("blocked_http")
    assert diag.detail.startswith("DART 연결: 실패 (요청 차단)")
    assert "http" not in diag.detail and "crtfc_key" not in diag.detail and "?" not in diag.detail
    tls = _dart_online_diag("tls")
    assert tls.detail.startswith("DART 연결: 실패 (보안 인증서 확인 실패)")
    raw = tls.detail[len("DART 연결: 실패 (보안 인증서 확인 실패)"):]
    assert len(raw) <= 80 + 3


def test_storage_failed_detail_drops_terminal_hint():
    diag = _dart_offline_diag("storage_failed")
    assert "dartlens-setup" not in (diag.detail or "")
    assert "claude_desktop_config" not in (diag.detail or "")
    assert (diag.detail or "").startswith("키 저장소: 사용할 수 없음")


# ---------------------------------------------------------------------------
# 3. dartlens_status (Claude 답변 안)
# ---------------------------------------------------------------------------

_CALL_STATUS = {"last_call_at": None, "last_status": None, "last_success_at": None}


@pytest.mark.parametrize("lic", ["missing", "invalid", "invalid_api_key", "expired", "revoked", "clock", "active"])
@pytest.mark.parametrize(
    "dart",
    ["missing", "storage_failed", "license_shape", "online:tls", "online:timeout", "online:dns",
     "online:rate_limited", "online:ip_blocked", "online:rejected", "online:service"],
)
def test_status_tool_output(monkeypatch, lic, dart):
    license_diag = _license_diag(monkeypatch, lic)
    api_diag = _dart_online_diag(dart.split(":", 1)[1]) if dart.startswith("online:") else _dart_offline_diag(dart)
    text = server._format_status(
        version="1.0.0", latest_version="1.1.0", license_diag=license_diag, api_diag=api_diag,
        cache_diag=_cache_diag("missing"), call_status=_CALL_STATUS, checked_online=True,
    )
    assert_customer_copy(text, f"dartlens_status {lic}/{dart}")
    if lic != "active":
        assert "LeetKit Manager" in text


def test_status_tool_uses_answer_prefix():
    api_diag = _dart_offline_diag("missing")
    lic = diagnostics.LicenseDiagnosis(status="missing", message="라이선스 키가 아직 없어요.")
    text = server._format_status(
        version="1.0.0", latest_version="1.0.0", license_diag=lic, api_diag=api_diag,
        cache_diag=_cache_diag("ok"), call_status=_CALL_STATUS, checked_online=False,
    )
    assert "LeetKit Manager의 DartLens 카드에서 [활성화]를 눌러 메일로 받은 키를 넣어주세요." in text
    assert (
        "DART 인증키가 아직 없어요. LeetKit Manager의 DartLens 카드에서 [활성화]를 눌러 DART 인증키를 넣어주세요."
        in text
    )


def test_status_docstring_does_not_send_claude_to_terminal():
    doc = server.dartlens_status.__doc__ or ""
    assert "dartlens-doctor" not in doc
    assert "[지원 문의]" in doc


# ---------------------------------------------------------------------------
# 4. 도구 오류 응답 (Claude 답변 안)
# ---------------------------------------------------------------------------


def test_missing_dart_key_tool_message(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setattr(_keyring, "load", lambda: None)
    with pytest.raises(_safe.MissingApiKeyError) as ctx:
        _safe.require_api_key()
    assert str(ctx.value) == (
        "DART 인증키가 아직 없어요. LeetKit Manager의 DartLens 카드에서 [활성화]를 눌러 DART 인증키를 넣어주세요."
    )


@pytest.mark.parametrize(
    "exc",
    [
        _safe.MissingApiKeyError("DART 인증키가 아직 없어요. LeetKit Manager의 DartLens 카드에서 [활성화]를 눌러 DART 인증키를 넣어주세요."),
        RuntimeError("boom"),
        KeyError("list"),
    ],
)
def test_safe_tool_outputs(monkeypatch, exc):
    monkeypatch.setattr(_safe, "is_licensed", lambda: True)

    @_safe.safe_tool
    async def tool():
        raise exc

    text = asyncio.run(tool())
    assert_customer_copy(text, f"safe_tool {type(exc).__name__}")


def test_unknown_error_does_not_blame_input(monkeypatch):
    monkeypatch.setattr(_safe, "is_licensed", lambda: True)

    @_safe.safe_tool
    async def tool():
        raise RuntimeError("boom")

    text = asyncio.run(tool())
    assert "입력값" not in text and "corp_code" not in text
    assert text.startswith(
        "⚠️ 조회 중 문제가 생겼어요. 같은 질문을 한 번 더 해보고, 그래도 같으면 LeetKit Manager 상단 [지원 문의]를 눌러주세요."
    )
    assert "RuntimeError" in text  # 원인은 한 줄로 남긴다(지원용)


def test_locked_tool_returns_locked_message(monkeypatch):
    monkeypatch.setattr(_safe, "is_licensed", lambda: False)
    monkeypatch.setattr(_safe, "locked_message", lambda: licensing.LOCKED_MESSAGE)

    @_safe.safe_tool
    async def tool():
        return "data"

    assert_customer_copy(asyncio.run(tool()), "locked tool")


# ---------------------------------------------------------------------------
# 5. Manager 활성화 창 사유 (activate --json / setup --json)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [licensing._REASON_MALFORMED, licensing._REASON_WRONG_PRODUCT, licensing._REASON_BAD_SIGNATURE, licensing._REASON_PUBKEY],
)
def test_activation_window_copy_for_verify_failures(reason):
    text = licensing.activation_failure_message({"valid": False, "reason": reason}, "SOMEKEY")
    assert_customer_copy(text, f"activate {reason}")
    assert "서명" not in text and "위조" not in text


def test_activation_window_copy_for_swapped_keys():
    text = licensing.activation_failure_message({"valid": False, "reason": licensing._REASON_MALFORMED}, "c" * 40)
    assert text == licensing.CROSS_HINT_API_KEY_IN_LICENSE_FIELD
    assert_customer_copy(text)
    assert_customer_copy(licensing.CROSS_HINT_LICENSE_IN_API_KEY_FIELD)


@pytest.mark.parametrize("case", ["trial_used", "expired", "revoked"])
def test_save_key_reasons(monkeypatch, case):
    monkeypatch.setattr(licensing, "verify_key", lambda k: {"valid": True, "license_id": "abcdef123456", "expires_on": None})
    monkeypatch.setattr(licensing, "_other_trial_used", lambda res: "ffffff000000" if case == "trial_used" else None)
    monkeypatch.setattr(licensing, "effective_expiry", lambda res: None)
    monkeypatch.setattr(licensing, "_is_expired", lambda e: case == "expired")
    monkeypatch.setattr(licensing, "is_revoked", lambda lid: case == "revoked")
    res = licensing.save_key("SOMEKEY")
    assert res["valid"] is False
    text = licensing.activation_failure_message(res, "SOMEKEY")
    assert_customer_copy(text, f"save_key {case}")
    assert "@" not in text


def test_activate_cli_json_message(monkeypatch, tmp_path):
    monkeypatch.setenv("DARTLENS_HOME", str(tmp_path))
    monkeypatch.delenv("DARTLENS_LICENSE_KEY", raising=False)
    for argv in (["NOT-A-REAL-KEY", "--json"], ["a" * 40, "--json"]):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["activate", *argv]), contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                licensing.activate_cli()
        result = json.loads(buf.getvalue())
        assert_customer_copy(result["message"], f"activate --json {argv[0][:6]}")


SETUP_CASES = {
    "timeout": {"side_effect": httpx.ReadTimeout("timed out")},
    "tls": {"side_effect": httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] x")},
    "rate": {"return_value": ("020", {"message": "제한"})},
    "service": {"return_value": ("800", {"message": "점검"})},
    "ip": {"return_value": ("012", {"message": "접근할 수 없는 IP입니다."})},
    "rejected": {"return_value": ("010", {"message": "등록되지 않은 키"})},
}


@pytest.mark.parametrize("case", list(SETUP_CASES))
def test_setup_json_messages(case):
    with patch.object(diagnostics, "check_dart_key_online", AsyncMock(**SETUP_CASES[case])), \
         patch.object(setup_claude, "_write_config_entry"):
        with pytest.raises(setup_claude.SetupError) as ctx:
            setup_claude.run_setup_noninteractive(api_key="a" * 40, targets=[])
    assert_customer_copy(ctx.value.message, f"setup {case}")


def test_setup_json_storage_failure_has_no_plaintext_flag():
    err = _keyring.KeyringUnavailableError("키체인 저장 실패: X\n  평문 모드로 저장하려면:\n    dartlens-setup --plaintext <KEY>")
    with patch.object(diagnostics, "check_dart_key_online", AsyncMock(return_value=("000", {}))), \
         patch.object(setup_claude.keyring_helper, "save", side_effect=err), \
         patch.object(setup_claude, "_write_config_entry"):
        with pytest.raises(setup_claude.SetupError) as ctx:
            setup_claude.run_setup_noninteractive(api_key="a" * 40, targets=[])
    assert_customer_copy(ctx.value.message, "setup storage")
    assert "--plaintext" not in ctx.value.message
    assert "[지원 문의]" in ctx.value.message


def test_setup_json_license_in_api_field():
    with patch.object(setup_claude, "_write_config_entry"):
        with pytest.raises(setup_claude.SetupError) as ctx:
            setup_claude.run_setup_noninteractive(api_key=_fake_license_shaped_key(), targets=[])
    assert_customer_copy(ctx.value.message, "setup license-in-api-field")
