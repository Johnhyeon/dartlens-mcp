"""Configure Claude Desktop to use dartlens.

Run `dartlens-setup` after `pip install dartlens-mcp`.

기본 동작 (권장):
    1. 키 입력받기 (인자 / DART_API_KEY env / 키체인에 이미 저장된 키 재사용 / 대화형)
    2. DART에 1회 호출해 키 유효성 검증 (삼성전자 corp_code 기준)
    3. 키를 OS 키체인에 저장 (Windows DPAPI / macOS Keychain / Secret Service)
    4. claude_desktop_config.json의 mcpServers.dartlens 엔트리 등록 — env에는 키를 두지 않음
    5. (마이그레이션) legacy `dart-mcp` 엔트리·평문 키는 자동 제거

평문 모드 (`--plaintext`):
    OS 키체인을 쓸 수 없는 환경(헤드리스 / 공유 계정 / 키체인이 막힌 정책)을 위한 fallback.
    이 경우 기존 동작처럼 env.DART_API_KEY를 JSON에 박는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path

import httpx

from dartlens import _keyring as keyring_helper
from dartlens import diagnostics
from dartlens import licensing

SERVER_KEY = "dartlens"
LEGACY_KEYS: list[str] = ["dart-mcp"]


class SetupError(Exception):
    """비대화형(--non-interactive/--json) 경로의 실패. print/sys.exit 대신 이걸 던진다."""

    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# 키 입력 / 검증
# ---------------------------------------------------------------------------

def _prompt_for_key() -> str:
    """DART API 키 입력 — getpass 로 화면 echo 차단 (어깨너머 노출 방지).

    터미널에 따라 getpass 가 fallback 이 안 될 수 있으므로 (e.g. dumb terminal)
    실패 시 일반 input 으로 떨어진다 — 그때만 평문 echo.
    """
    import getpass

    print()
    print("DART OpenAPI 키를 입력하세요. (입력값은 화면에 표시되지 않음)")
    print("(키가 없다면 https://opendart.fss.or.kr 에서 무료 발급)")
    print()
    try:
        try:
            key = getpass.getpass("DART_API_KEY: ").strip()
        except (getpass.GetPassWarning, OSError):
            # tty 가 아니거나 echo 끄기 실패 → 평문 fallback (경고 표시)
            print("  [WARN] echo 끄기 실패 — 입력값이 화면에 보일 수 있습니다.")
            key = input("DART_API_KEY: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("입력이 취소되었습니다.")
    return key


async def _validate_key_async(api_key: str) -> tuple[bool, str]:
    """DART 라이트 엔드포인트 1회 호출로 키를 검증. HTTP 메커니즘은 diagnostics 모듈과 공유."""
    try:
        status, data = await diagnostics.check_dart_key_online(api_key)
        if status in diagnostics.SUCCESS_CODES:
            if status == "013":
                return True, "검증 성공 (조회 결과 없음)"
            corp_name = data.get("corp_name", "(unknown)")
            return True, f"검증 성공 — {corp_name}"
        return False, f"DART 응답 [{status}]: {data.get('message', '알 수 없는 오류')}"
    except httpx.HTTPError as e:
        return False, f"네트워크 오류: {type(e).__name__}: {e}"
    except Exception as e:
        return False, f"오류: {type(e).__name__}: {e}"


def validate_key(api_key: str) -> tuple[bool, str]:
    return asyncio.run(_validate_key_async(api_key))


# ---------------------------------------------------------------------------
# Claude Desktop config 위치 / entry 결정
# ---------------------------------------------------------------------------

def _uv_tool_bin_dirs() -> list[Path]:
    """`uv tool install`이 entry point를 배치하는 경로 후보.

    uv는 `~/.local/bin` (Unix·Windows 공통)을 표준으로 쓰지만, 사용자가
    `UV_TOOL_BIN_DIR` / `XDG_BIN_HOME`로 재정의할 수 있다. 두 경우 다 커버.
    """
    candidates: list[Path] = []
    env = os.environ.get("UV_TOOL_BIN_DIR")
    if env:
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_BIN_HOME")
    if xdg:
        candidates.append(Path(xdg))
    candidates.append(Path.home() / ".local" / "bin")
    return [p for p in candidates if p.exists()]


def resolve_server_entry(preferred_command: str = "dartlens") -> dict:
    """PATH 의존 없이 확실히 실행되는 MCP server config entry를 생성.

    우선순위:
    1. 절대 경로가 명시되면 그대로 사용
    2. uv tool bin 디렉토리 (`~/.local/bin` 등) — Manager 가 갱신하는 대상
    3. PATH 탐색 (shutil.which)
    4. sysconfig scripts 디렉토리 직접 탐색 (pip 호환)
    5. 최후 fallback: `python -m dartlens`
    """
    if os.path.isabs(preferred_command) and Path(preferred_command).exists():
        return {"command": preferred_command}

    # uv tool bin 디렉토리를 **PATH 보다 먼저** 본다.
    #
    # 실기기에서 확인한 사고: 옛 `pip install` 잔재가 시스템 Python 의 Scripts\ 에 남아
    # 있으면 PATH 순서상 그게 먼저 잡혀서, 설정 파일에 옛 실행 파일 경로가 박힌다. 그러면
    # Manager 로 최신 버전을 올려도 호스트 앱(Claude·ChatGPT)은 계속 옛 버전을 띄운다 —
    # "업데이트했는데 그대로다" 가 되고, 원인이 설정 파일 안에 있어서 찾기도 어렵다.
    # uv 가 관리하는 쪽이 Manager 가 실제로 갱신하는 대상이므로 그쪽을 먼저 쓴다.
    # (uv 없이 pip 로만 설치한 환경은 이 디렉토리가 없어 아래 PATH 탐색으로 내려간다.)
    for bin_dir in _uv_tool_bin_dirs():
        for candidate_name in (f"{preferred_command}.exe", preferred_command):
            candidate = bin_dir / candidate_name
            if candidate.exists():
                return {"command": str(candidate)}

    found = shutil.which(preferred_command)
    if found:
        return {"command": found}

    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for candidate_name in (f"{preferred_command}.exe", preferred_command):
            candidate = scripts_dir / candidate_name
            if candidate.exists():
                return {"command": str(candidate)}
    except Exception:
        pass

    return {
        "command": sys.executable,
        "args": ["-m", "dartlens"],
    }


def _find_store_config_path() -> Path | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    packages_dir = Path(local_appdata) / "Packages"
    if not packages_dir.exists():
        return None
    for pattern in ("Claude_*", "*Claude*"):
        for pkg in packages_dir.glob(pattern):
            candidate = pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            if candidate.parent.exists():
                return candidate
    return None


def get_claude_desktop_config_path() -> Path:
    """Claude Desktop 앱의 mcpServers config 파일 경로."""
    if sys.platform == "win32":
        store = _find_store_config_path()
        if store is not None:
            return store
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not found.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def get_claude_code_config_path() -> Path:
    """Claude Code CLI의 사용자 스코프 config (`~/.claude.json`).

    Claude Code도 Claude Desktop과 동일하게 mcpServers 객체를 사용한다.
    파일에는 사용자 설정·세션 등 다른 키가 같이 들어있을 수 있어 mcpServers
    부분만 patching 한다.
    """
    return Path.home() / ".claude.json"


def get_codex_config_path() -> Path:
    """Codex CLI의 MCP 서버 설정 — `~/.codex/config.toml`, `[mcp_servers.<name>]` 섹션.

    Windows에서 실제 설치본으로 이 경로를 확인했다. macOS/Linux도 관례상 같은 경로일
    가능성이 높으나 이 환경에서 직접 검증하지는 못했다.
    """
    return Path.home() / ".codex" / "config.toml"


# 하위 호환 — 기존 import 자리 유지
def get_config_path() -> Path:
    return get_claude_desktop_config_path()


# (target name, 경로 함수, 사람이 읽는 라벨)
TARGETS: dict[str, tuple] = {
    "claude-desktop": (get_claude_desktop_config_path, "Claude Desktop"),
    "claude-code": (get_claude_code_config_path, "Claude Code CLI"),
    "codex": (get_codex_config_path, "Codex CLI"),
}


# ---------------------------------------------------------------------------
# config 갱신
# ---------------------------------------------------------------------------

def _backup_and_load(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        backup = config_path.with_suffix(".json.backup")
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"  [OK] Backup saved: {backup}")
        return config
    except json.JSONDecodeError:
        print("  [WARN] Existing config is corrupted. Creating new one.")
        return {}


def _store_api_key(api_key: str, *, plaintext: bool, env_for_entry: dict) -> dict | None:
    """API 키 저장 정책. 반환값은 entry에 박을 env dict (없으면 None).

    keyring 모드: env에서 DART_API_KEY 제거, keyring에 저장.
    plaintext 모드: env에 DART_API_KEY 박고 keyring 항목 삭제 (일관성).
    """
    env = dict(env_for_entry)
    if plaintext:
        env["DART_API_KEY"] = api_key
        if keyring_helper.delete():
            print("  [OK] Removed previous keyring entry (plaintext mode chosen)")
        print("  [WARN] PLAINTEXT mode — DART_API_KEY is stored unencrypted in JSON config")
        return env or None

    had_plain = "DART_API_KEY" in env
    env.pop("DART_API_KEY", None)
    if had_plain:
        print("  [OK] Migrated: removed plaintext DART_API_KEY from JSON config")

    try:
        backend = keyring_helper.save(api_key)
        print(f"  [OK] Stored in OS keychain ({backend})")
    except keyring_helper.KeyringUnavailableError as e:
        # 메시지 자체에 이미 --plaintext 안내가 포함되어 있음 (중복 블록 제거)
        print(f"  [ERROR] {e}", file=sys.stderr)
        raise SystemExit(2)
    return env or None


def _toml_env_and_write(doc, *, api_key: str, command: str, plaintext: bool, existing_env: dict) -> dict:
    """TOML 문서에 mcp_servers.dartlens 테이블을 채워 넣는다(파일 쓰기는 호출자가 함).
    JSON 쪽 `_store_api_key`와 같은 정신 — plaintext면 env에 키를 남기고, 아니면
    키체인에만 둔다(TOML에는 아예 env 자체를 안 만듦)."""
    import tomlkit

    entry = resolve_server_entry(command)
    server_table = tomlkit.table()
    server_table["command"] = entry["command"]
    if "args" in entry:
        server_table["args"] = entry["args"]
    if plaintext:
        env_table = tomlkit.table()
        env_table["DART_API_KEY"] = api_key
        server_table["env"] = env_table
    doc["mcp_servers"][SERVER_KEY] = server_table
    return entry


def _configure_toml_target(
    config_path: Path, label: str, *, api_key: str, command: str, plaintext: bool,
) -> None:
    """Codex처럼 TOML 기반인 타겟용 — tomlkit으로 다른 mcp_servers.*·주석은 보존."""
    import tomlkit

    print()
    print(f"  → {label}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    if config_path.exists():
        backup = config_path.with_suffix(".toml.backup")
        backup.write_bytes(config_path.read_bytes())
        print(f"  [OK] Backup saved: {backup}")
        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            print("  [WARN] Existing config is corrupted. Creating new one.")
            doc = tomlkit.document()

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table()
    for legacy in LEGACY_KEYS:
        if legacy in doc["mcp_servers"]:
            del doc["mcp_servers"][legacy]
            print(f"  [OK] Removed legacy entry: {legacy}")

    entry = _toml_env_and_write(doc, api_key=api_key, command=command, plaintext=plaintext, existing_env={})
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    print(f"  [OK] Config updated (key: {SERVER_KEY})")
    print(f"  Path:    {config_path}")
    print(f"  Command: {entry['command']}")
    if "args" in entry:
        print(f"  Args:    {' '.join(entry['args'])}")
    if plaintext:
        print(f"  Env:     DART_API_KEY=***{api_key[-4:]} (plaintext)")
    else:
        print("  Env:     (none — DART_API_KEY in keychain)")


def _configure_one_target(
    config_path: Path,
    label: str,
    *,
    api_key: str,
    command: str,
    plaintext: bool,
    save_key: bool,
) -> None:
    """단일 config 파일(Claude Desktop 또는 Claude Code)에 mcpServers.dartlens 등록."""
    if config_path.suffix == ".toml":
        # keyring 저장 자체는 save_key일 때 딱 한 번만 하면 되므로(여러 타겟 순회 시
        # claude-desktop 등에서 이미 처리됨) 여기선 config 파일 갱신만 담당.
        if save_key:
            _store_api_key(api_key, plaintext=plaintext, env_for_entry={})
        _configure_toml_target(config_path, label, api_key=api_key, command=command, plaintext=plaintext)
        return

    print()
    print(f"  → {label}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = _backup_and_load(config_path)
    config.setdefault("mcpServers", {})

    for legacy in LEGACY_KEYS:
        if legacy in config["mcpServers"]:
            del config["mcpServers"][legacy]
            print(f"  [OK] Removed legacy entry: {legacy}")

    entry = resolve_server_entry(command)
    existing = config["mcpServers"].get(SERVER_KEY) or {}
    existing_env = dict(existing.get("env") or {})

    # 키 저장은 첫 타겟에서만 (여러 타겟이라도 keyring 한 번이면 충분)
    if save_key:
        env = _store_api_key(api_key, plaintext=plaintext, env_for_entry=existing_env)
    else:
        env = existing_env if plaintext else (
            {k: v for k, v in existing_env.items() if k != "DART_API_KEY"} or None
        )
        # plaintext 모드에서 동일 키를 모든 타겟 entry에 박아두기
        if plaintext:
            env = dict(env or {})
            env["DART_API_KEY"] = api_key

    if env:
        entry["env"] = env

    config["mcpServers"][SERVER_KEY] = entry

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print(f"  [OK] Config updated (key: {SERVER_KEY})")
    print(f"  Path:    {config_path}")
    print(f"  Command: {entry['command']}")
    if "args" in entry:
        print(f"  Args:    {' '.join(entry['args'])}")
    if "env" in entry and entry["env"]:
        if plaintext:
            print(f"  Env:     DART_API_KEY=***{api_key[-4:]} (plaintext)")
        else:
            print(f"  Env:     {list(entry['env'].keys())} (no DART_API_KEY — keychain)")
    else:
        print("  Env:     (none — DART_API_KEY in keychain)")

    cmd = entry["command"]
    if Path(cmd).is_absolute() and not Path(cmd).exists():
        print(f"  [WARN] Recorded command file does not exist: {cmd}")
    elif not Path(cmd).is_absolute() and not shutil.which(cmd):
        print(f"  [WARN] '{cmd}' not found in PATH.")


def configure(
    api_key: str,
    *,
    command: str = "dartlens",
    plaintext: bool,
    targets: list[str] | None = None,
) -> None:
    """선택된 모든 타겟에 dartlens MCP 등록.

    targets: ["claude-desktop"], ["claude-code"], 또는 ["claude-desktop", "claude-code"].
    기본값은 ["claude-desktop"] (하위 호환).
    """
    targets = targets or ["claude-desktop"]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {unknown}. Valid: {list(TARGETS.keys())}")

    for i, target in enumerate(targets):
        path_func, label = TARGETS[target]
        _configure_one_target(
            path_func(),
            label,
            api_key=api_key,
            command=command,
            plaintext=plaintext,
            save_key=(i == 0),  # 키 저장은 첫 타겟에서만
        )


# ---------------------------------------------------------------------------
# 비대화형 경로 (Manager 등 --json/--non-interactive) — print/sys.exit 없는 순수 로직
# ---------------------------------------------------------------------------

def _write_config_entry_toml(config_path: Path, *, api_key: str, command: str, plaintext: bool) -> dict:
    """`_write_config_entry`의 TOML(Codex 등) 버전 — 조용히(print 없이) 수행."""
    import tomlkit

    config_path.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    if config_path.exists():
        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            doc = tomlkit.document()
        backup = config_path.with_suffix(".toml.backup")
        backup.write_bytes(config_path.read_bytes())

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table()
    for legacy in LEGACY_KEYS:
        doc["mcp_servers"].pop(legacy, None)

    entry = _toml_env_and_write(doc, api_key=api_key, command=command, plaintext=plaintext, existing_env={})
    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return entry


def _write_config_entry(config_path: Path, *, api_key: str, command: str, plaintext: bool) -> dict:
    """`_configure_one_target`의 출력-없는 버전. stdout에는 최종 JSON 한 줄만 나가야 하므로
    backup/legacy 제거/entry 기록을 조용히 수행한다."""
    if config_path.suffix == ".toml":
        return _write_config_entry_toml(config_path, api_key=api_key, command=command, plaintext=plaintext)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            backup = config_path.with_suffix(".json.backup")
            with open(backup, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            config = {}

    config.setdefault("mcpServers", {})
    for legacy in LEGACY_KEYS:
        config["mcpServers"].pop(legacy, None)

    entry = resolve_server_entry(command)
    if plaintext:
        entry["env"] = {"DART_API_KEY": api_key}

    config["mcpServers"][SERVER_KEY] = entry
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return entry


def run_setup_noninteractive(
    *,
    api_key: str,
    targets: list[str],
    command: str = "dartlens",
    plaintext_consent: bool = False,
) -> dict:
    """`--non-interactive`/`--json` 진입점의 순수 로직. print/sys.exit 없음 — 실패 시 SetupError.

    Manager 계약: 반환 dict는 항상 `api_key_saved` 키를 쓴다 (`license_activated`와 혼용 안 함).
    """
    key = (api_key or "").strip()
    if not key:
        # 명시적으로 준 키가 없으면 키체인에 이미 등록된 키를 재사용 시도 — 타겟 재등록/
        # 마이그레이션 목적의 재실행(예: legacy 엔트리 정리)에서 매번 재입력을 요구하지 않는다.
        key = (keyring_helper.load() or "").strip()
    if not key:
        # 키가 없어도 MCP 등록은 해준다.
        #
        # 예전엔 여기서 실패시켰다 — 그래서 Manager의 "MCP 등록" 버튼이 DartLens에서만
        # 세 타겟 모두 실패했다(다른 두 Lens는 자격증명 없이도 등록된다). 사용자 눈에는
        # 이유 없이 등록이 안 되는 막다른 길이었다.
        #
        # 등록과 키 입력은 별개 작업이다. 등록만 해두면 클라이언트가 서버를 띄울 수 있고,
        # 키가 없는 동안에는 각 도구가 "키를 넣으라"고 자기 자리에서 안내한다 —
        # StockLens가 라이선스 없이 등록된 뒤 도구에서 안내하는 것과 같은 모양이다.
        for target in targets:
            path_func, _label = TARGETS[target]
            _write_config_entry(path_func(), api_key="", command=command, plaintext=False)
        return {
            "ok": True,
            "api_key_saved": False,
            "storage": None,
            "targets": list(targets),
            "key_tail_masked": None,
            "validated_online": False,
            "message": "MCP 등록을 마쳤습니다. DART API 키는 아직 등록되지 않았습니다.",
        }

    if licensing.looks_like_license_shape(key):
        raise SetupError(diagnostics.DART_API_KEY_INVALID, licensing.CROSS_HINT_LICENSE_IN_API_KEY_FIELD)

    try:
        status, data = asyncio.run(diagnostics.check_dart_key_online(key))
    except httpx.TimeoutException:
        raise SetupError(diagnostics.DART_NETWORK_UNREACHABLE, "DART 서버 응답이 지연되고 있습니다 (타임아웃).")
    except httpx.ConnectError:
        raise SetupError(diagnostics.DART_NETWORK_UNREACHABLE, "DART 서버에 연결할 수 없습니다. 인터넷 연결을 확인하세요.")
    except httpx.HTTPError as e:
        raise SetupError(diagnostics.DART_NETWORK_UNREACHABLE, f"네트워크 오류: {type(e).__name__}")

    if status in diagnostics.RATE_LIMIT_CODES:
        raise SetupError(
            diagnostics.DART_API_RATE_LIMITED,
            f"DART 요청 제한에 도달했습니다 (응답 {status}). 키는 정상일 수 있습니다 — 잠시 후 다시 시도하세요.",
        )
    if status in diagnostics.SERVICE_ISSUE_CODES:
        raise SetupError(
            diagnostics.DART_NETWORK_UNREACHABLE,
            f"DART 서비스 자체 문제로 보입니다 (응답 {status}). 키 문제가 아닙니다.",
        )
    if status not in diagnostics.SUCCESS_CODES:
        raise SetupError(
            diagnostics.DART_API_KEY_INVALID,
            f"DART가 키를 거부했습니다 (응답 {status}: {data.get('message', '알 수 없는 오류')}).",
        )

    plaintext = False
    storage = "os-keychain"
    try:
        keyring_helper.save(key)
    except keyring_helper.KeyringUnavailableError as e:
        if not plaintext_consent:
            raise SetupError(
                diagnostics.DART_API_KEY_STORAGE_FAILED,
                f"OS 키체인을 사용할 수 없습니다: {e} 평문 저장에 동의하려면 --plaintext 를 추가하세요.",
            )
        plaintext = True
        storage = "plaintext-config"

    for target in targets:
        path_func, _label = TARGETS[target]
        _write_config_entry(path_func(), api_key=key, command=command, plaintext=plaintext)

    return {
        "api_key_saved": True,
        "storage": storage,
        "targets": list(targets),
        "key_tail_masked": licensing.mask_tail(key),
        "validated_online": True,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _remove_one_target(config_path, label: str) -> dict:
    """한 설정 파일에서 우리 MCP 항목만 지운다. 다른 서버 항목·주석은 안 건드린다.

    파일이 없거나 항목이 없으면 "이미 없음"으로 성공 처리한다 — 해제를 두 번 눌러도
    실패로 보이면 안 된다. 지우기 전에 항상 백업을 남긴다(등록 때와 같은 규칙).
    """
    from pathlib import Path as _Path

    config_path = _Path(config_path)
    result = {"target_label": label, "config_path": str(config_path), "removed": False}
    if not config_path.exists():
        return result

    if config_path.suffix == ".toml":
        import tomlkit

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            return result
        config_path.with_suffix(".toml.backup").write_bytes(config_path.read_bytes())
        servers = doc.get("mcp_servers")
        if servers is not None:
            for key in [SERVER_KEY, *LEGACY_KEYS]:
                if servers.pop(key, None) is not None:
                    result["removed"] = True
        if result["removed"]:
            config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return result

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return result
    with open(config_path.with_suffix(".json.backup"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    servers = config.get("mcpServers") or {}
    for key in [SERVER_KEY, *LEGACY_KEYS]:
        if servers.pop(key, None) is not None:
            result["removed"] = True
    if result["removed"]:
        config["mcpServers"] = servers
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    return result


def remove(targets: list[str]) -> list[dict]:
    """선택한 타겟들에서 MCP 등록을 해제한다.

    Manager의 "MCP 등록" 모달에서 체크를 풀면 여기로 온다. 예전엔 해제 수단이 아예
    없어서, 체크박스가 토글처럼 보이는데 실제로는 "추가만" 됐다 — 체크를 풀고 등록을
    눌러도 그 설정이 그대로 남아 사용자 눈에는 먹통으로 보였다.
    """
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {unknown}. Valid: {list(TARGETS.keys())}")
    results = []
    for target in targets:
        path_func, label = TARGETS[target]
        entry = _remove_one_target(path_func(), label)
        entry["target"] = target
        results.append(entry)
    return results


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dartlens-setup",
        description="Register dartlens in Claude config (Desktop and/or Code CLI) and store the DART API key.",
    )
    p.add_argument(
        "api_key",
        nargs="?",
        help="DART OpenAPI 키. 생략 시 대화형 입력 또는 DART_API_KEY 환경변수.",
    )
    p.add_argument(
        "--target",
        choices=["claude-desktop", "claude-code", "both", "auto", "codex"],
        default="auto",
        help=(
            "MCP 등록 대상. "
            "claude-desktop=Claude Desktop 앱, claude-code=Claude Code CLI, "
            "both=둘 다, auto=환경 자동 감지 (기본: auto), "
            "codex=ChatGPT 앱·Codex CLI(같은 ~/.codex/config.toml 을 읽는다. "
            "auto 는 Claude 가 하나도 없을 때 이쪽을 고른다). "
            "DARTLENS_TARGET 환경변수로도 지정 가능."
        ),
    )
    p.add_argument(
        "--command",
        default="dartlens",
        help="MCP 클라이언트가 실행할 커맨드 (기본: dartlens).",
    )
    p.add_argument(
        "--plaintext",
        action="store_true",
        help=(
            "OS 키체인 대신 config env에 키를 평문 저장 (헤드리스 환경 fallback). "
            "--non-interactive 모드에서는 키체인 실패 시 평문 저장에 동의한다는 의미도 겸한다."
        ),
    )
    p.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="DART API 키를 위치인자/환경변수 대신 stdin에서 읽음 (argv/프로세스 목록 노출 방지).",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="프롬프트 없이 즉시 성공/실패 (Manager 등 자동화용). 재시도 루프 없음.",
    )
    p.add_argument(
        "--remove",
        action="store_true",
        help="등록을 해제한다(설정 파일에서 우리 항목만 삭제). Manager의 체크 해제가 이걸 쓴다.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="사람용 출력 대신 JSON 결과 한 줄만 stdout에 출력. --non-interactive를 암시.",
    )
    return p


def _has_codex() -> bool:
    """codex 타겟을 쓸 수 있는 환경인지 — Codex CLI 또는 ChatGPT 앱.
    통합 이후 둘은 같은 `~/.codex/config.toml` 을 읽으므로 하나로 본다.

    ChatGPT 앱을 깔았지만 MCP 설정을 한 번도 안 건드린 사람은 이 폴더가 없을 수 있다 —
    그때는 auto 판정이 결국 claude-desktop 으로 떨어진다(앱 자체를 찾아내는 일은 OS별
    설치 경로 탐색이 필요해서 LeetKit Manager 쪽이 담당한다)."""
    if shutil.which("codex"):
        return True
    return get_codex_config_path().parent.exists()


def _resolve_targets(arg: str) -> list[str]:
    """`--target` 인자를 실제 타겟 리스트로 해석. `auto`는 환경 감지."""
    if arg == "both":
        return ["claude-desktop", "claude-code"]
    if arg in TARGETS:
        return [arg]
    if arg == "auto":
        # env 우선
        env_target = (os.environ.get("DARTLENS_TARGET") or "").strip().lower()
        if env_target and env_target != "auto":
            return _resolve_targets(env_target)

        # 자동 감지:
        #   1. `claude` CLI 존재 = Claude Code 사용 환경
        #   2. Claude Desktop config 디렉토리 존재 = Desktop 사용 환경
        #   3. 둘 다면 both
        #   4. Claude 가 하나도 없으면 codex (ChatGPT 앱·Codex CLI가 같은 설정을 읽는다)
        #   5. 아무것도 못 찾으면 claude-desktop (가장 흔한 케이스)
        has_code = shutil.which("claude") is not None
        desktop_dir = get_claude_desktop_config_path().parent
        has_desktop = desktop_dir.exists()

        if has_code and has_desktop:
            return ["claude-desktop", "claude-code"]
        if has_code:
            return ["claude-code"]
        if has_desktop:
            return ["claude-desktop"]
        # ChatGPT 만 쓰는 사람에게 **없는 앱의 설정 파일**을 만들고 "등록 완료"라고
        # 말하던 자리다 — 시킨 대로 다 했는데 도구가 안 보인다.
        if _has_codex():
            return ["codex"]
        return ["claude-desktop"]
    raise ValueError(f"Invalid target: {arg}")


_MAX_KEY_ATTEMPTS = 3


def _try_reuse_stored_key() -> str | None:
    """키체인에 이미 저장된 키가 있으면 재입력 없이 재사용을 시도한다.

    dartlens-setup을 "새 키 등록"이 아니라 "타겟 재등록/마이그레이션" 목적으로 다시
    돌리는 흔한 케이스(예: legacy `dart-mcp` 엔트리를 `dartlens`로 정리)에서, 예전에
    등록한 키를 사용자가 기억 못 해도 막히지 않게 한다. 검증까지 1회 거친 뒤 성공한
    키만 반환 — 없거나 검증 실패면 None을 반환해 호출자가 기존 프롬프트 흐름으로
    자연스럽게 폴백하게 한다.
    """
    stored = (keyring_helper.load() or "").strip()
    if not stored:
        return None
    print()
    print("  이미 등록된 DART API 키를 키체인에서 발견했습니다 — 재입력 없이 재사용을 시도합니다.")
    ok, msg = validate_key(stored)
    if ok:
        print(f"  [OK] {msg}")
        return stored
    print(f"  [WARN] 저장된 키 검증 실패 — {msg}")
    print("  새 키를 입력해주세요.")
    return None


def _obtain_validated_key(initial_key: str, *, key_was_provided: bool) -> str:
    """키 입력 → DART 검증 루프. 실패 시 재시도 (최대 _MAX_KEY_ATTEMPTS).

    key_was_provided: True 면 사용자가 args/env 로 키를 명시한 경우.
        그땐 첫 시도만 하고 실패하면 즉시 종료 (자동화 흐름 방해 안 함).
    """
    api_key = initial_key
    for attempt in range(1, _MAX_KEY_ATTEMPTS + 1):
        if not api_key:
            try:
                api_key = _prompt_for_key()
            except SystemExit:
                raise
        if not api_key:
            print("  [ERROR] DART API 키가 입력되지 않았습니다.", file=sys.stderr)
            sys.exit(1)

        print()
        print(f"  Validating API key against DART... (attempt {attempt}/{_MAX_KEY_ATTEMPTS})")
        ok, msg = validate_key(api_key)
        if ok:
            print(f"  [OK] {msg}")
            return api_key

        print(f"  [ERROR] 키 검증 실패: {msg}", file=sys.stderr)
        print(
            "  키가 올바른지, DART(https://opendart.fss.or.kr)에서 발급받은 키인지 확인하세요.",
            file=sys.stderr,
        )

        # 사용자가 args/env 로 키를 줬으면 자동화 흐름이라 prompt 띄우지 않음
        if key_was_provided and attempt == 1:
            sys.exit(2)

        if attempt >= _MAX_KEY_ATTEMPTS:
            print(f"  [ERROR] {_MAX_KEY_ATTEMPTS}회 모두 실패. 종료합니다.", file=sys.stderr)
            sys.exit(2)

        # 다음 attempt 는 다시 prompt 받기
        api_key = ""
        key_was_provided = False  # 첫 attempt 후에는 항상 대화형으로 재시도

    sys.exit(2)


def _decide_plaintext_mode(explicit_flag: bool) -> bool:
    """키체인 응답성을 실제 측정해서 평문 모드 적용 여부 결정.

    우선순위:
    1. 사용자가 `--plaintext` 명시 → True
    2. `DARTLENS_NO_PLAINTEXT=1` 명시 → False (키체인 강제 시도, 실패해도 fallback 안 함)
    3. Windows/macOS → False (DPAPI/Keychain 신뢰)
    4. Linux: keyring.get_password 를 짧은 timeout 으로 찔러봐서:
       - 응답 있음 → False (키체인 정상)
       - 응답 없음/예외 → tty면 y/N으로 명시적 동의를 구함(동의해야만 True).
         동의 안 함/비tty → False로 반환해 실제 keychain 저장을 시도하게 두고,
         거기서 다시 실패하면 기존 KeyringUnavailableError → SystemExit(2) 안내
         (--plaintext 재실행)로 자연스럽게 떨어진다. 동의 없이 평문으로
         자동 전환하지 않는다.

    DISPLAY env 휴리스틱은 RasPi OS Desktop 같은 케이스(DISPLAY 는 있지만 실제
    SecretService 는 잠금)를 못 잡아내므로 실제 응답성 측정으로 대체.
    """
    if explicit_flag:
        return True
    if os.environ.get("DARTLENS_NO_PLAINTEXT"):
        return False
    if sys.platform != "linux":
        return False

    print()
    print("  Probing OS keychain (Linux Secret Service)...")
    if keyring_helper.is_responsive():
        print("  [OK] Keychain responsive — using OS keychain mode")
        return False

    print("  [WARN] OS 키체인이 응답하지 않습니다 (헤드리스 SSH/RaspberryPi 등에서 흔함).")
    print("         동의 없이 평문 저장으로 자동 전환하지 않습니다 — 아래에서 직접 결정하세요.")

    if sys.stdin.isatty():
        try:
            ans = input(
                "  평문 모드(키가 config JSON에 그대로 저장됨)로 진행하는 데 동의하십니까? (y/N) ▸ "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans == "y":
            print("  [OK] 평문 모드에 동의함 — config JSON 의 env 항목에 저장합니다.")
            for path in (
                Path.home() / ".claude.json",
                Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
            ):
                if path.exists() or path.parent.exists():
                    print(f"         저장 후 파일 권한을 닫아주세요: chmod 600 {path}")
            return True

    print("  [INFO] 평문 저장에 동의하지 않아 키체인 저장을 그대로 시도합니다.")
    print("         (실패하면 명시적으로 --plaintext 를 붙여 재실행하라는 안내가 나옵니다)")
    return False


def main() -> None:
    # 파이프/리다이렉트로 stdout이 콘솔이 아니게 되면 Windows는 cp949 등 로캘 코드페이지로
    # 떨어져 em-dash 같은 문자에서 UnicodeEncodeError가 난다. Manager가 --json 출력을
    # 파이프로 읽는 게 기본 사용 패턴이라 이 reconfigure가 없으면 JSON 계약 자체가 깨진다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = _build_parser().parse_args()
    targets = _resolve_targets(args.target)

    # 해제는 등록과 완전히 다른 일이라 여기서 바로 갈라진다 — API 키 검증 같은 등록
    # 절차를 탈 이유가 없다(키가 없어도 해제는 돼야 한다).
    if args.remove:
        try:
            removed = remove(targets)
        except Exception as e:  # noqa: BLE001 — 실패도 JSON 계약으로 알려야 한다
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            else:
                print(f"  [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False))
        elif not args.non_interactive:
            for entry in removed:
                state = "해제됨" if entry["removed"] else "원래 없음"
                print(f"  [OK] {entry['target_label']}: {state}")
        sys.exit(0)

    if args.json or args.non_interactive:
        api_key = (
            sys.stdin.read().strip()
            if args.api_key_stdin
            else (args.api_key or os.environ.get("DART_API_KEY", "")).strip()
        )
        try:
            result = run_setup_noninteractive(
                api_key=api_key,
                targets=targets,
                command=args.command,
                plaintext_consent=args.plaintext,
            )
            code = 0
        except SetupError as e:
            result = {"api_key_saved": False, "error_code": e.error_code, "message": e.message}
            code = 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message") or ("완료" if result.get("api_key_saved") else "실패"), file=sys.stderr)
        sys.exit(code)

    print("==============================================")
    print("  dartlens — MCP Setup")
    print("==============================================")

    target_labels = ", ".join(TARGETS[t][1] for t in targets)
    print(f"  Targets: {target_labels}")

    plaintext = _decide_plaintext_mode(args.plaintext)

    initial_key = (args.api_key or os.environ.get("DART_API_KEY", "")).strip()
    key_was_provided = bool(initial_key)
    reused_key = None if initial_key else _try_reuse_stored_key()

    api_key = reused_key or _obtain_validated_key(initial_key, key_was_provided=key_was_provided)

    try:
        configure(
            api_key,
            command=args.command,
            plaintext=plaintext,
            targets=targets,
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"  [ERROR] {e}", file=sys.stderr)
        sys.exit(3)

    print()
    if "claude-desktop" in targets:
        print("Done! Claude Desktop을 완전히 종료(트레이→Quit) 후 다시 실행하세요.")
    if "claude-code" in targets:
        print("Done! Claude Code 세션에서 자동 적용 — 새 세션부터 dartlens 도구 사용 가능.")


if __name__ == "__main__":
    main()
