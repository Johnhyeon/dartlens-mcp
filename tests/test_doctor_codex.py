"""Codex(TOML 기반 MCP 클라이언트) 등록 상태를 doctor가 실제로 감지하는지 검증 —
DartLens.

실사용 중 발견된 문제 재현: setup_claude.py는 Phase C에서 --target codex를 지원하게
됐지만, doctor.py는 여전히 claude-desktop/claude-code(JSON)만 확인하고 있어서 실제로
Codex에 정상 등록해도 Manager 대시보드의 targets/체크박스에는 전혀 반영되지 않았다.
"""

from __future__ import annotations

import tomlkit

from dartlens import doctor


def test_check_config_codex_info_skip_when_file_missing(tmp_path):
    missing = tmp_path / "config.toml"
    result = doctor._check_config_toml_file("Codex CLI", missing, required=False)
    assert result.status == "info-skip"


def test_check_config_codex_ok_when_entry_present(tmp_path):
    config_path = tmp_path / "config.toml"
    fake_exe = tmp_path / "dartlens.exe"
    fake_exe.write_text("", encoding="utf-8")
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table()
    server = tomlkit.table()
    server["command"] = str(fake_exe)
    doc["mcp_servers"][doctor.SERVER_KEY] = server
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    result = doctor._check_config_toml_file("Codex CLI", config_path, required=False)
    assert result.status == "ok"


def test_check_config_codex_preserves_other_entries_untouched(tmp_path):
    """다른 MCP 서버 항목이 섞여 있어도 우리 항목만 보고 판단 — 남의 서버가
    등록 안 돼 있다고 우리 상태까지 fail로 착각하면 안 된다."""
    config_path = tmp_path / "config.toml"
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table()
    other = tomlkit.table()
    other["command"] = "npx"
    doc["mcp_servers"]["github"] = other
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    result = doctor._check_config_toml_file("Codex CLI", config_path, required=False)
    assert result.status == "info-skip"  # dartlens 엔트리 자체가 없음(github만 있음)


def test_registered_targets_includes_codex_when_configured():
    desktop_check = doctor.Check("d")
    desktop_check.status = "fail"
    code_check = doctor.Check("c")
    code_check.status = "fail"
    codex_check = doctor.Check("x")
    codex_check.status = "ok"

    targets = doctor._registered_targets(desktop_check, code_check, codex_check)
    assert targets == ["codex"]


def test_check_at_least_one_config_ok_when_only_codex_registered():
    desktop_check = doctor.Check("d")
    desktop_check.status = "fail"
    code_check = doctor.Check("c")
    code_check.status = "fail"
    codex_check = doctor.Check("x")
    codex_check.status = "ok"

    result = doctor.check_at_least_one_config(desktop_check, code_check, codex_check)
    assert result.status == "ok"
