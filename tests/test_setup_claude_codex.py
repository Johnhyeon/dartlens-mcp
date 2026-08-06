"""Codex(TOML 기반 MCP 클라이언트) 등록 경로 단위 테스트 — DartLens.

`_write_config_entry`(Manager의 --api-key-stdin --json --non-interactive 경로가 실제로
쓰는 함수)가 TOML로 올바르게 분기하는지, 기존 항목·주석을 보존하는지 검증한다.
실제 keyring/네트워크에 의존하지 않게 필요한 부분만 monkeypatch한다.
"""

from __future__ import annotations

from unittest.mock import patch

import tomlkit

from dartlens import setup_claude


def test_get_codex_config_path_is_under_home_dot_codex():
    assert setup_claude.get_codex_config_path().parts[-2:] == (".codex", "config.toml")


def test_codex_added_to_targets_dict():
    assert "codex" in setup_claude.TARGETS
    path_func, label = setup_claude.TARGETS["codex"]
    assert path_func() == setup_claude.get_codex_config_path()
    assert label == "Codex CLI"


def test_target_choices_include_codex():
    parser = setup_claude._build_parser()
    target_action = next(a for a in parser._actions if a.dest == "target")
    assert "codex" in target_action.choices


def test_write_config_entry_toml_preserves_other_entries_and_comments(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# a comment that must survive\n"
        "[mcp_servers.github]\n"
        'command = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-github"]\n',
        encoding="utf-8",
    )

    with patch.object(setup_claude, "resolve_server_entry", return_value={"command": "/fake/dartlens"}):
        entry = setup_claude._write_config_entry(config_path, api_key="FAKE-KEY", command="dartlens", plaintext=False)

    text = config_path.read_text(encoding="utf-8")
    assert "# a comment that must survive" in text
    doc = tomlkit.parse(text)
    assert doc["mcp_servers"]["github"]["command"] == "npx"
    assert doc["mcp_servers"]["dartlens"]["command"] == "/fake/dartlens"
    assert "env" not in doc["mcp_servers"]["dartlens"]  # keyring 모드 — 평문 키 안 남음
    assert entry["command"] == "/fake/dartlens"
    assert (tmp_path / "config.toml.backup").exists()


def test_write_config_entry_toml_plaintext_mode_embeds_key(tmp_path):
    config_path = tmp_path / "config.toml"
    with patch.object(setup_claude, "resolve_server_entry", return_value={"command": "/fake/dartlens"}):
        setup_claude._write_config_entry(config_path, api_key="FAKE-KEY", command="dartlens", plaintext=True)

    doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    assert doc["mcp_servers"]["dartlens"]["env"]["DART_API_KEY"] == "FAKE-KEY"


def test_write_config_entry_dispatches_toml_by_suffix(tmp_path):
    config_path = tmp_path / "config.toml"
    with patch.object(setup_claude, "_write_config_entry_toml", return_value={"command": "x"}) as mock_toml:
        result = setup_claude._write_config_entry(config_path, api_key="k", command="dartlens", plaintext=False)
    mock_toml.assert_called_once()
    assert result == {"command": "x"}
