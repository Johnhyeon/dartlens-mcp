"""RECENT_TOOL_FAILURES — 최근 48시간 도구 호출 기록으로 '아직 풀리지 않은 실패'를 본다.

실제 사용자 폴더(~/.dartlens/logs)는 읽지도 쓰지도 않는다 — 기록 폴더를 tmp 로 돌린다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from dartlens import _metrics, diagnostics, doctor


def _row(tool, minutes_ago, error=None, detail=None, now=None):
    now = now or datetime(2026, 9, 17, 12, 0, 0)
    return {
        "timestamp": (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds"),
        "tool": tool,
        "duration_ms": 10.0,
        "output_chars": 0 if error else 100,
        "error": error,
        "error_detail": detail,
    }


def test_no_records():
    d = diagnostics.diagnose_recent_tool_failures([])
    assert d.status == "ok"
    assert d.summary == "최근 이틀 동안 AI 앱이 DartLens를 쓴 기록이 없어요."
    assert d.error_code is None


def test_all_success():
    rows = [_row("search_company", 30), _row("list_disclosures", 20), _row("search_company", 10)]
    d = diagnostics.diagnose_recent_tool_failures(rows)
    assert d.status == "ok"
    assert d.summary == "최근 이틀 동안 조회 3번이 모두 정상이었어요."


def test_failure_then_same_tool_success_is_resolved():
    rows = [
        _row("search_company", 30, "ConnectError", "[Errno 11001] getaddrinfo failed"),
        _row("search_company", 20),
        _row("list_disclosures", 10),
    ]
    d = diagnostics.diagnose_recent_tool_failures(rows)
    assert d.status == "ok"
    assert d.summary == "최근 이틀 동안 조회 3번 중 1번이 실패했지만, 계속 실패하고 있지는 않아요."
    assert d.action is None
    assert any("search_company: 실패 1번" in line and "분류 dns" in line for line in d.lines)


def test_last_call_failed_is_warn_with_category_action():
    rows = [
        _row("search_company", 40),
        _row("list_disclosures", 30, "ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer"),
        _row("get_major_accounts", 20, "ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] x"),
        _row("search_company", 10),
    ]
    d = diagnostics.diagnose_recent_tool_failures(rows)
    assert d.status == "warn"
    assert d.summary == "최근 조회 중 아직 실패로 남아 있는 것이 2가지 있어요."
    assert d.error_code == "RECENT_TOOL_FAILURES_TLS"
    assert "백신이나 회사 보안 프로그램" in d.action
    assert "[지원 문의]" in d.action
    line = next(line for line in d.lines if line.startswith("get_major_accounts"))
    assert line.startswith("get_major_accounts: 실패 1번, 마지막 11:40, 분류 tls, ConnectError: ")


def test_single_unclear_failure_is_not_warn_but_repeat_is():
    """AI 앱이 인자를 한 번 잘못 넣은 호출로 카드가 이틀 내내 '주의'가 되면 안 된다.
    다시 불러도 또 실패하면 그때는 진짜 결함으로 본다."""
    once = [_row("list_disclosures", 30, "ValueError", "page_no는 1 이상의 정수여야 합니다 (받음: 0).")]
    d = diagnostics.diagnose_recent_tool_failures(once)
    assert d.status == "ok"
    assert any(line.startswith("list_disclosures: 실패 1번") for line in d.lines)

    twice = once + [_row("list_disclosures", 20, "ValueError", "page_no는 1 이상의 정수여야 합니다 (받음: 0).")]
    d = diagnostics.diagnose_recent_tool_failures(twice)
    assert d.status == "warn"
    assert d.error_code == "RECENT_TOOL_FAILURES_OTHER"


def test_cancelled_only_is_not_a_failure():
    rows = [
        _row("get_full_financial", 30, "CancelledError", None),
        _row("get_full_financial", 10, "CancelledError", None),
    ]
    d = diagnostics.diagnose_recent_tool_failures(rows)
    assert d.status == "ok"
    assert d.summary == "최근 이틀 동안 조회 2번이 모두 정상이었어요."
    # 실패로 세지 않지만 지원용 details 에는 남긴다.
    assert any("get_full_financial: 취소 2번" in line and "cancelled" in line for line in d.lines)


def test_cancel_after_failure_does_not_hide_the_failure():
    rows = [
        _row("search_company", 30, "ReadTimeout", "timed out"),
        _row("search_company", 20, "ReadTimeout", "timed out"),
        _row("search_company", 10, "CancelledError", None),
    ]
    d = diagnostics.diagnose_recent_tool_failures(rows)
    assert d.status == "warn"
    assert d.error_code == "RECENT_TOOL_FAILURES_TIMEOUT"


def test_details_mask_keys_and_cap_lines():
    key = "a1b2c3d4" * 5  # 40자리 hex (DART 인증키 모양)
    rows = [_row(f"tool_{i}", 60 - i * 2 - j, "RuntimeError", f"boom {key}") for i in range(12) for j in range(2)]
    d = diagnostics.diagnose_recent_tool_failures(rows)
    assert len(d.lines) <= 8
    assert key not in json.dumps(d.lines, ensure_ascii=False)
    assert d.error_code == "RECENT_TOOL_FAILURES_OTHER"


# ── 기록 읽기 ─────────────────────────────────────────────────────────────


def _write(folder, day, rows):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"metrics_{day:%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write("{broken json\n")


def test_loader_reads_only_last_48_hours(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, 12, 0, 0)
    logs = tmp_path / "logs"
    monkeypatch.setattr(_metrics, "_metrics_dir_readonly", lambda: logs)
    _write(logs, now - timedelta(days=3), [_row("old", 60 * 72, now=now)])
    _write(logs, now - timedelta(days=2), [_row("edge_out", 60 * 49, now=now), _row("edge_in", 60 * 47, now=now)])
    _write(logs, now, [_row("today", 5, now=now)])

    rows = _metrics.load_recent_metrics(48, now=now)
    assert [r["tool"] for r in rows] == ["edge_in", "today"]


def test_loader_does_not_create_folders(tmp_path, monkeypatch):
    logs = tmp_path / "nothing" / "logs"
    monkeypatch.setattr(_metrics, "_metrics_dir_readonly", lambda: logs)
    assert _metrics.load_recent_metrics(48) == []
    assert not logs.exists()


# ── doctor JSON 계약 ──────────────────────────────────────────────────────


def _report_with_rows(monkeypatch, tmp_path, rows):
    logs = tmp_path / "logs"
    monkeypatch.setattr(_metrics, "_metrics_dir_readonly", lambda: logs)
    today = datetime.now()
    fixed = [dict(r, timestamp=(today - timedelta(minutes=5 - i)).isoformat(timespec="seconds")) for i, r in enumerate(rows)]
    _write(logs, today, fixed)
    state = doctor.run_diagnostics(online=False)
    return doctor.build_report(state, online=False)


def test_doctor_json_includes_check_non_critical(monkeypatch, tmp_path):
    report = _report_with_rows(
        monkeypatch, tmp_path, [_row("search_company", 0, "DartApiError", "[020] 요청 제한을 초과했습니다")]
    )
    check = next(c for c in report["checks"] if c["id"] == "RECENT_TOOL_FAILURES")
    assert check["status"] == "warn"
    assert check["critical"] is False
    assert check["details"]["error_code"] == "RECENT_TOOL_FAILURES_BLOCKED"  # StockLens·TelegramLens 와 같은 자리
    assert "[지원 문의]" in check["action"]
    # 기존 최상위 필드·검사 ID 는 그대로다.
    ids = [c["id"] for c in report["checks"]]
    for existing in ("UV_AVAILABLE", "PACKAGE_IMPORTABLE", "COMMAND_AVAILABLE", "MCP_CONFIG_VALID",
                     "DART_API_KEY", "LICENSE_ACTIVE", "CORP_CODE_CACHE"):
        assert existing in ids
    assert report["overall"] in ("ok", "degraded", "fail")


def test_doctor_json_ok_check_has_no_error_code(monkeypatch, tmp_path):
    report = _report_with_rows(monkeypatch, tmp_path, [_row("search_company", 0)])
    check = next(c for c in report["checks"] if c["id"] == "RECENT_TOOL_FAILURES")
    assert check["status"] == "ok"
    assert check["critical"] is False
    assert "error_code" not in check


def test_doctor_survives_loader_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(_metrics, "load_recent_metrics", boom)
    report = doctor.build_report(doctor.run_diagnostics(online=False), online=False)
    check = next(c for c in report["checks"] if c["id"] == "RECENT_TOOL_FAILURES")
    assert check["status"] == "info-skip"


@pytest.fixture(autouse=True)
def _no_real_dart_key(monkeypatch):
    # run_diagnostics 는 OS 키체인을 읽는다. 결과와 무관하게 키 원문이 섞이지 않게 env 로 고정.
    monkeypatch.setenv("DART_API_KEY", "b" * 40)
