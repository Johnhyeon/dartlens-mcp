"""dartlens 설치·설정 진단 도구.

실행: `dartlens-doctor` 또는 `python -m dartlens.doctor`

체크 항목:
- uv 설치 여부 (Python 런타임 관리자)
- dartlens-mcp 패키지 import 가능 여부
- dartlens 실행 명령 탐색 (PATH / uv tool bin / sysconfig)
- Claude Desktop config 파일
- config 내 dartlens entry 유효성 (command resolvable)
- Legacy 키 잔존 여부
- DART API 키 출처 (env / keychain) — 키 자체는 출력하지 않음
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import sysconfig
from datetime import datetime, timezone
from pathlib import Path

try:
    from dartlens.setup_claude import (
        get_claude_desktop_config_path,
        get_claude_code_config_path,
        get_codex_config_path,
        SERVER_KEY,
        LEGACY_KEYS,
        _uv_tool_bin_dirs,
        _find_store_config_path,
    )
    from dartlens import diagnostics
    from dartlens import _corp_code
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from dartlens.setup_claude import (
        get_claude_desktop_config_path,
        get_claude_code_config_path,
        get_codex_config_path,
        SERVER_KEY,
        LEGACY_KEYS,
        _uv_tool_bin_dirs,
        _find_store_config_path,
    )
    from dartlens import diagnostics
    from dartlens import _corp_code


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = None  # "ok" / "warn" / "fail"
        self.lines: list[str] = []
        self.fix: str | None = None
        # ok/warn/fail 중 실제로 상태를 확정한 호출의 메시지만 담는다(.info()는 제외) —
        # Manager 계약 checks[].summary는 이 한 줄이고, 나머지 info 라인은 details.lines로 간다.
        self.summary: str = ""

    def ok(self, msg: str):
        self.status = "ok"
        self.summary = msg
        self.lines.append(msg)
        return self

    def warn(self, msg: str, fix: str | None = None):
        if self.status != "fail":
            self.status = "warn"
            self.summary = msg
        self.lines.append(msg)
        if fix:
            self.fix = fix
        return self

    def fail(self, msg: str, fix: str | None = None):
        self.status = "fail"
        self.summary = msg
        self.lines.append(msg)
        if fix:
            self.fix = fix
        return self

    def info(self, msg: str):
        self.lines.append(msg)
        return self

    def to_contract_dict(self, check_id: str, *, repairable: bool = False, repair_id: str | None = None) -> dict:
        """Manager 공통 계약(checks[] 항목) 형태로 변환."""
        details: dict = {}
        if self.lines:
            details["lines"] = list(self.lines)
        return {
            "id": check_id,
            "status": self.status,
            "summary": self.summary or (self.lines[0] if self.lines else ""),
            "details": details,
            "repairable": repairable,
            "repair_id": repair_id,
            "action": self.fix,
        }

    def to_dict(self) -> dict:
        d: dict = {"status": self.status, "lines": list(self.lines)}
        if self.fix:
            d["fix"] = self.fix
        return d


def _find_uv() -> str | None:
    """uv 실행 파일 경로. PATH뿐 아니라 설치 스크립트가 실제로 두는 위치까지 본다.

    PATH만 보면 실사용에서 오탐이 난다 — LeetKit Manager가 uv를 자동 설치해주면 uv는
    `~/.local/bin`에 생기지만 설치 스크립트는 *영구* PATH(레지스트리)만 갱신하므로,
    이미 실행 중인 프로세스에는 반영되지 않는다. 그 상태로 PATH만 확인하면 설치가
    멀쩡히 끝났는데도 계속 "uv 없음" 경고가 떠서 카드가 영영 "주의"로 남는다.
    """
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for bin_dir in (home / ".local" / "bin", home / ".cargo" / "bin"):
        for name in ("uv.exe", "uv"):
            candidate = bin_dir / name
            if candidate.exists():
                return str(candidate)
    return None


def check_uv() -> Check:
    c = Check("uv (Python runtime manager)")
    uv = _find_uv()
    if uv:
        c.ok("uv is installed")
        c.info(f"Path:       {uv}")
    else:
        c.warn(
            "uv not found",
            fix=(
                "Install uv (recommended):\n"
                "  Windows: irm https://astral.sh/uv/install.ps1 | iex\n"
                "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
            ),
        )
    return c


def check_package() -> Check:
    c = Check("Package (dartlens-mcp)")
    try:
        import dartlens  # noqa: F401
        c.ok("dartlens-mcp is importable")
        c.info(f"Location:   {Path(dartlens.__file__).parent}")
        c.info(f"Version:    {dartlens.__version__}")
        c.info(f"Python:     {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        c.info(f"Executable: {sys.executable}")
    except ImportError:
        c.fail(
            "dartlens-mcp NOT importable in current interpreter",
            fix="uv tool install --force dartlens-mcp",
        )
    return c


def check_dartlens_command() -> Check:
    c = Check("Command (dartlens)")
    exe = shutil.which("dartlens")
    if exe:
        c.ok("'dartlens' found in PATH")
        c.info(f"Path:       {exe}")
        return c

    for bin_dir in _uv_tool_bin_dirs():
        for name in ("dartlens.exe", "dartlens"):
            candidate = bin_dir / name
            if candidate.exists():
                # PATH에 없는 것 자체는 문제가 아니다 — MCP 등록은 절대경로로 하므로
                # 그대로 동작한다. 예전엔 warn이라 카드가 영영 "주의"로 남았는데,
                # 정작 안내문에 "무시 가능"이라고 적혀 있는 경고였다.
                c.ok("'dartlens' is installed")
                c.info(f"Path:       {candidate}")
                c.info("Not on PATH — MCP registration uses this absolute path, so no action is needed.")
                c.info(f'Add "{bin_dir}" to PATH only if you want to type the command in a terminal.')
                return c

    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for name in ("dartlens.exe", "dartlens"):
            candidate = scripts_dir / name
            if candidate.exists():
                c.warn(
                    "'dartlens' exists in sysconfig scripts but not on PATH",
                    fix=f'Add to PATH: "{scripts_dir}"',
                )
                c.info(f"Path:       {candidate}")
                return c
    except Exception:
        pass

    c.fail(
        "'dartlens' command NOT found anywhere",
        fix="uv tool install --force dartlens-mcp",
    )
    return c


def _check_config_file(label: str, config_path: Path, *, required: bool) -> Check:
    """단일 config 파일에 대한 점검. required=False면 부재 시 fail 대신 info."""
    c = Check(f"Config — {label}")

    if "Packages" in str(config_path) and "LocalCache" in str(config_path):
        c.info("Detected: Microsoft Store version (sandboxed path)")
    c.info(f"Path:       {config_path}")

    if not config_path.exists():
        if required:
            c.fail("Config file does not exist", fix="dartlens-setup")
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
            c.summary = "Config file does not exist (target not in use — OK)"
        return c

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        c.fail(f"Config is not valid JSON: {e}", fix="Back up and re-run dartlens-setup")
        return c
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcpServers", {}) or {}
    entry = servers.get(SERVER_KEY)

    legacy_found = [k for k in LEGACY_KEYS if k in servers]
    if legacy_found:
        c.warn(
            f"Legacy entries present: {legacy_found}",
            fix="dartlens-setup (auto-removes)",
        )

    if not entry:
        # legacy 엔트리가 있다 = 이전에 이 클라이언트로 dartlens(또는 dart-mcp)를 쓰고 있었다 →
        # 마이그레이션 미완료 상태라 fail.
        if required or legacy_found:
            msg = (
                f"'{SERVER_KEY}' entry missing in mcpServers"
                + (f" (legacy {legacy_found} present)" if legacy_found else "")
            )
            c.fail(
                msg,
                fix=f"dartlens-setup --target {label_to_target(label)}",
            )
        else:
            c.info(f"'{SERVER_KEY}' entry not present (target not in use — OK)")
            c.status = "info-skip"
            c.summary = f"'{SERVER_KEY}' entry not present (target not in use — OK)"
        return c

    cmd = entry.get("command")
    args = entry.get("args", [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.fail(f"Command file missing: {cmd}", fix="dartlens-setup")
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.fail(
                f"Command '{cmd}' not in PATH — client will fail to launch the server",
                fix="dartlens-setup",
            )

    return c


def label_to_target(label: str) -> str:
    return "claude-code" if "Code" in label else "claude-desktop"


def check_config_desktop() -> Check:
    return _check_config_file(
        "Claude Desktop", get_claude_desktop_config_path(), required=False
    )


def check_config_code() -> Check:
    return _check_config_file(
        "Claude Code CLI", get_claude_code_config_path(), required=False
    )


def check_config_codex() -> Check:
    return _check_config_toml_file(
        "Codex CLI", get_codex_config_path(), required=False
    )


def _check_config_toml_file(label: str, config_path: Path, *, required: bool) -> Check:
    """TOML 기반 클라이언트(Codex의 `~/.codex/config.toml`, `[mcp_servers.<key>]`)용
    config 점검 — _check_config_file(JSON)과 같은 계약(entry 유무·command 유효성)을
    TOML 구조에 맞춰 재구현. setup_claude._configure_toml_target()이 쓰는 것과 동일한
    구조(mcp_servers.<SERVER_KEY> = {command, args?})를 읽기만 한다."""
    c = Check(f"Config — {label}")
    c.info(f"Path:       {config_path}")

    if not config_path.exists():
        if required:
            c.fail("Config file does not exist", fix="dartlens-setup --target codex")
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
            c.summary = "Config file does not exist (target not in use — OK)"
        return c

    try:
        import tomlkit

        with open(config_path, "r", encoding="utf-8") as f:
            cfg = tomlkit.parse(f.read())
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcp_servers", {}) or {}
    entry = servers.get(SERVER_KEY)

    if not entry:
        if required:
            c.fail(f"'{SERVER_KEY}' entry missing in mcp_servers", fix="dartlens-setup --target codex")
        else:
            c.info(f"'{SERVER_KEY}' entry not present (target not in use — OK)")
            c.status = "info-skip"
            c.summary = f"'{SERVER_KEY}' entry not present (target not in use — OK)"
        return c

    cmd = entry.get("command")
    args = list(entry.get("args") or [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.fail(f"Command file missing: {cmd}", fix="dartlens-setup --target codex")
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.fail(
                f"Command '{cmd}' not in PATH — client will fail to launch the server",
                fix="dartlens-setup --target codex",
            )

    return c


def check_at_least_one_config(*configs: Check) -> Check:
    """모든 config가 미등록이면 종합 fail. 하나라도 등록돼있으면 OK."""
    c = Check("Registered targets")
    registered = [
        cc for cc in configs
        if cc.status == "ok" or (cc.status == "warn" and "Legacy" in " ".join(cc.lines))
    ]
    if registered:
        c.ok(f"{len(registered)} target(s) configured")
        return c
    c.fail(
        "dartlens not registered in any MCP client (Claude Desktop / Code / Codex)",
        fix="dartlens-setup --target {claude-desktop|claude-code|both|codex}",
    )
    return c


def _registered_targets(desktop_check: Check, code_check: Check, codex_check: Check) -> list[str]:
    """실제로 등록된 MCP 타겟 slug 목록 — Manager 공통 계약 top-level `targets` 필드용."""
    targets: list[str] = []
    if desktop_check.status == "ok" or (desktop_check.status == "warn" and "Legacy" in " ".join(desktop_check.lines)):
        targets.append("claude-desktop")
    if code_check.status == "ok" or (code_check.status == "warn" and "Legacy" in " ".join(code_check.lines)):
        targets.append("claude-code")
    if codex_check.status == "ok":
        targets.append("codex")
    return targets


# doctor.py의 Check 키 -> Manager 공통 계약 checks[].id. StockLens와 개념이 겹치는
# 항목(PACKAGE_IMPORTABLE/COMMAND_AVAILABLE/MCP_CONFIG_VALID/LICENSE_ACTIVE)은
# 이름을 맞추고, DartLens 고유 항목은 여기서만 쓰는 이름을 붙인다.
_CHECK_IDS = {
    "uv": "UV_AVAILABLE",
    "package": "PACKAGE_IMPORTABLE",
    "command": "COMMAND_AVAILABLE",
    "config_desktop": "MCP_CONFIG_DESKTOP",
    "config_code": "MCP_CONFIG_CODE",
    "config_codex": "MCP_CONFIG_CODEX",
    "registered_targets": "MCP_CONFIG_VALID",
    "dart_api": "DART_API_KEY",
    "license": "LICENSE_ACTIVE",
    "corp_code_cache": "CORP_CODE_CACHE",
}


def _extract_plaintext_dart_key() -> str | None:
    """Claude 설정 JSON(Desktop/Code)에 --plaintext 모드로 박힌 DART_API_KEY를 찾는다.

    diagnostics.diagnose_dart_api_key()는 config 파일을 직접 읽지 않으므로(단일 책임),
    doctor.py가 이미 하고 있는 config 파싱에서 값을 뽑아 넘겨준다.
    """
    for path in (get_claude_desktop_config_path(), get_claude_code_config_path()):
        try:
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            entry = (cfg.get("mcpServers", {}) or {}).get(SERVER_KEY) or {}
            key = ((entry.get("env") or {}).get("DART_API_KEY") or "").strip()
            if key:
                return key
        except Exception:
            continue
    return None


def _dart_api_check_from_diag(diag: "diagnostics.DartApiDiagnosis") -> Check:
    c = Check("DART API Key")
    if diag.storage:
        c.info(f"Storage:    {diag.storage}")
    if diag.key_tail_masked:
        c.info(f"Tail:       {diag.key_tail_masked}")
    if diag.status == "valid":
        c.ok(diag.message or "DART API 키가 등록되어 있습니다.")
    elif diag.status in ("rate_limited", "network_unreachable"):
        c.warn(diag.message, fix="잠시 후 다시 시도하세요 (일시적 문제 — 키 설정 문제 아님)")
    elif diag.status == "storage_failed":
        c.fail(diag.message, fix="dartlens-setup --plaintext <YOUR_DART_API_KEY>")
    else:  # missing, invalid
        c.fail(diag.message, fix="dartlens-setup <YOUR_DART_API_KEY>")
    return c


def _license_check_from_diag(diag: "diagnostics.LicenseDiagnosis") -> Check:
    c = Check("DartLens License")
    if diag.license_id_masked:
        c.info(f"License ID: {diag.license_id_masked}")
    if diag.status == "active":
        c.ok(diag.message or "라이선스가 활성화되어 있습니다.")
    else:
        c.fail(diag.message, fix="dartlens-activate <라이선스-키>")
    return c


def check_dart_api_key() -> Check:
    """DART API 키 진단 (오프라인) — diagnostics.diagnose_dart_api_key()에 위임."""
    diag = diagnostics.diagnose_dart_api_key(config_plaintext_key=_extract_plaintext_dart_key())
    return _dart_api_check_from_diag(diag)


def check_license() -> Check:
    """DartLens 상품 라이선스 진단 — diagnostics.diagnose_license()에 위임."""
    return _license_check_from_diag(diagnostics.diagnose_license())


def _corp_cache_check_from_diag(diag: dict) -> Check:
    c = Check("Corp Code Cache")

    if not diag["exists"]:
        # 첫 설치엔 당연히 없고, 첫 조회 때 알아서 받는다 — 사용자가 할 일이 없는데도
        # 경고로 잡으면 갓 설치한 사람에게 "문제 1건"으로 보인다(TelegramLens의 DB
        # 미생성 판정과 같은 부류의 오해). 손상·권한 문제는 아래에서 계속 걸러진다.
        c.ok("기업코드 캐시는 첫 조회 때 자동으로 받습니다 — 지금은 준비 안 돼 있어도 정상입니다")
        return c

    c.info(f"Last updated: {diag['last_updated']}")
    if diag["entry_count"] is not None:
        c.info(f"Entries:      {diag['entry_count']:,}")

    if not diag["parseable"]:
        c.fail("캐시 파일이 손상되어 파싱할 수 없습니다", fix="dartlens-doctor --repair corp-code-cache --yes")
        return c

    if not diag["writable"]:
        c.fail("캐시 디렉토리에 쓰기 권한이 없어 갱신할 수 없습니다", fix="캐시 디렉토리 권한을 확인하세요")
        return c

    if not diag["is_fresh"]:
        c.warn("캐시가 오래되었습니다 (TTL 7일 초과)", fix="dartlens-doctor --repair corp-code-cache --yes")
        return c

    c.ok("corp code 캐시가 최신 상태입니다")
    return c


def _corp_cache_error_code(diag: dict) -> str | None:
    if not diag["exists"] or diag["parseable"] is False:
        return diagnostics.CORP_CODE_CACHE_MISSING
    if not diag["is_fresh"]:
        return diagnostics.CORP_CODE_CACHE_STALE
    return None


def check_corp_code_cache() -> Check:
    """corp code 캐시 진단 — _corp_code.cache_diagnosis()에 위임."""
    return _corp_cache_check_from_diag(_corp_code.cache_diagnosis())


STATUS_ICON = {
    "ok": "[ OK ]",
    "warn": "[WARN]",
    "fail": "[FAIL]",
    "info-skip": "[SKIP]",
    None: "[ ?  ]",
}


def print_check(c: Check):
    icon = STATUS_ICON.get(c.status, "[ ?  ]")
    print(f"{icon} {c.name}")
    for line in c.lines:
        print(f"       {line}")
    if c.fix:
        print(f"       Fix: {c.fix}")
    print()


def _package_version() -> str:
    try:
        import dartlens

        return dartlens.__version__
    except Exception:
        return "unknown"


def run_diagnostics(*, online: bool = False) -> dict:
    """전체 체크를 한 번 계산. 텍스트 모드/JSON 모드가 결과를 공유 — 중복 네트워크 호출 없음."""
    desktop_check = check_config_desktop()
    code_check = check_config_code()
    codex_check = check_config_codex()

    plaintext_key = _extract_plaintext_dart_key()
    if online:
        async def _online_gather():
            return await asyncio.gather(
                diagnostics.diagnose_dart_api_key_online(config_plaintext_key=plaintext_key),
                diagnostics.fetch_latest_pypi_version(),
            )

        dart_diag, latest_version = asyncio.run(_online_gather())
    else:
        dart_diag = diagnostics.diagnose_dart_api_key(config_plaintext_key=plaintext_key)
        latest_version = None
    license_diag = diagnostics.diagnose_license()
    corp_cache_diag = _corp_code.cache_diagnosis()

    checks: dict[str, Check] = {
        "uv": check_uv(),
        "package": check_package(),
        "command": check_dartlens_command(),
        "config_desktop": desktop_check,
        "config_code": code_check,
        "config_codex": codex_check,
        "registered_targets": check_at_least_one_config(desktop_check, code_check, codex_check),
        "dart_api": _dart_api_check_from_diag(dart_diag),
        "license": _license_check_from_diag(license_diag),
        "corp_code_cache": _corp_cache_check_from_diag(corp_cache_diag),
    }
    return {
        "checks": checks,
        "dart_diag": dart_diag,
        "license_diag": license_diag,
        "corp_cache_diag": corp_cache_diag,
        "targets": _registered_targets(desktop_check, code_check, codex_check),
        "latest_version": latest_version,
    }


def build_report(state: dict, *, online: bool) -> dict:
    """run_diagnostics() 결과를 Manager 공통 계약(schema_version/product/package_name/
    installed_version/latest_version/update_available/overall/checked_at/license/targets/checks)
    JSON으로 조립한다. dart_api/corp_code_cache는 DartLens 고유 확장 필드로 추가 유지.
    API 키/라이선스 키 원문은 어디에도 담기지 않는다."""
    checks = state["checks"]
    any_fail = any(c.status == "fail" for c in checks.values())
    any_warn = any(c.status == "warn" for c in checks.values())
    overall = "fail" if any_fail else ("degraded" if any_warn else "ok")

    corp_cache_diag = state["corp_cache_diag"]
    corp_cache_error_code = _corp_cache_error_code(corp_cache_diag)
    corp_cache_repairable = corp_cache_error_code is not None

    installed_version = _package_version()
    latest_version = state.get("latest_version")
    update_available = (
        diagnostics._version_gt(latest_version, installed_version) if latest_version else None
    )

    checks_list = []
    for key, c in checks.items():
        if key == "corp_code_cache":
            checks_list.append(
                c.to_contract_dict(
                    _CHECK_IDS[key],
                    repairable=corp_cache_repairable,
                    repair_id="corp-code-cache" if corp_cache_repairable else None,
                )
            )
        else:
            checks_list.append(c.to_contract_dict(_CHECK_IDS[key]))

    return {
        "schema_version": diagnostics.SCHEMA_VERSION,
        "product": diagnostics.PRODUCT,
        "package_name": diagnostics.PACKAGE_NAME,
        "installed_version": installed_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "overall": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "online": online,
        "license": state["license_diag"].to_dict(),
        "targets": state["targets"],
        "checks": checks_list,
        "dart_api": state["dart_diag"].to_dict(),
        "corp_code_cache": {**corp_cache_diag, "error_code": corp_cache_error_code},
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dartlens-doctor",
        description="dartlens 설치·설정 진단 도구.",
    )
    p.add_argument("--json", action="store_true", help="사람용 텍스트 대신 JSON 결과를 출력 (Manager 등 자동화용)")
    p.add_argument(
        "--online",
        action="store_true",
        help="DART 라이트 엔드포인트를 1회 호출해 API 키의 실제 유효성까지 확인 (기본은 존재/형식만 확인)",
    )
    p.add_argument(
        "--repair",
        choices=["corp-code-cache"],
        help="지정한 대상을 복구한다 (현재 corp-code-cache만 지원). 전체 진단은 건너뛴다.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="--repair 의 파괴적 작업(캐시 재다운로드) 실행에 동의. 없으면 안내만 하고 아무것도 바꾸지 않음.",
    )
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 서버와 같은 TLS 기준으로 진단한다. 어긋나면 진단은 통과하는데 서버만 실패하는,
    # 가장 찾기 어려운 상태가 된다 — 2026-08-13 문의에서 실제로 그랬다.
    from dartlens import _tls

    _tls.apply()

    args = _build_arg_parser().parse_args()

    if args.repair:
        try:
            result = _corp_code.repair_corp_code_cache(yes=args.yes)
        except Exception as e:
            result = {"repaired": False, "message": f"복구 중 예상치 못한 오류: {type(e).__name__}: {e}"}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message") or ("복구 완료" if result.get("repaired") else "복구 실패"))
        sys.exit(0 if result.get("repaired") or not args.yes else 1)

    try:
        state = run_diagnostics(online=args.online)
    except Exception as e:
        message = f"진단 중 예상치 못한 오류: {type(e).__name__}: {e}"
        if args.json:
            print(json.dumps({
                "schema_version": diagnostics.SCHEMA_VERSION,
                "product": diagnostics.PRODUCT,
                "package_name": diagnostics.PACKAGE_NAME,
                "overall": "fail",
                "error": message,
            }, ensure_ascii=False))
        else:
            print(f"[FAIL] {message}", file=sys.stderr)
        sys.exit(1)
    checks = state["checks"]

    if args.json:
        report = build_report(state, online=args.online)
        print(json.dumps(report, ensure_ascii=False))
        sys.exit(1 if report["overall"] == "fail" else 0)

    print("=" * 60)
    print("  dartlens Doctor - Installation Diagnosis")
    print("=" * 60)
    print()

    for c in checks.values():
        print_check(c)

    any_fail = any(c.status == "fail" for c in checks.values())
    any_warn = any(c.status == "warn" for c in checks.values())

    print("=" * 60)
    if any_fail:
        print("  [FAIL] One or more critical issues found.")
        print("  Apply the 'Fix:' commands above, then re-run dartlens-doctor.")
        sys.exit(1)
    elif any_warn:
        print("  [WARN] Installation works but some warnings exist.")
        print("  If MCP appears in Claude Desktop, you're fine.")
    else:
        print("  [ OK ] All checks passed!")
        print("  If MCP still doesn't appear, FULLY QUIT Claude Desktop")
        print("  (tray icon -> Quit) and restart.")
    print("=" * 60)


if __name__ == "__main__":
    main()
