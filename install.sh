#!/bin/sh
# dartlens Installer (macOS / Linux) — POSIX sh
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.sh | sh
#
# Or with API key prefilled (no prompt):
#   curl -LsSf https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.sh | DART_API_KEY=xxxx sh
#
# 3 steps:
#   1) uv (Python package manager) — auto-installs Python runtime if missing
#   2) dartlens-mcp via `uv tool install`
#   3) Claude Desktop config + DART API key validation via `dartlens-setup`
#
# 본 스크립트는 Debian/Ubuntu/RaspberryPiOS 의 dash 등 POSIX sh 에서도
# 그대로 동작하도록 작성되어 있다 (echo -e / arrays / [[ ]] 사용 안 함).

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

case "$(uname -s)" in
    Darwin) OS="macOS" ;;
    Linux)  OS="Linux" ;;
    *)      OS="Unknown" ;;
esac

printf '\n'
printf '==============================================\n'
printf '  dartlens Installer (%s)\n' "$OS"
printf '==============================================\n\n'

LOCAL_BIN="$HOME/.local/bin"

# ── [1/3] uv ─────────────────────────────────────────────
printf '%b[1/3] Checking uv...%b\n' "$CYAN" "$NC"
if ! command -v uv > /dev/null 2>&1; then
    printf '      uv not found. Installing from astral.sh...\n'
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        printf '      %b[FAIL] uv installation failed.%b\n' "$RED" "$NC"
        printf '      Manual install: https://docs.astral.sh/uv/getting-started/installation/\n'
        exit 1
    fi
    if [ -d "$LOCAL_BIN" ]; then
        PATH="$LOCAL_BIN:$PATH"
        export PATH
    fi
    if ! command -v uv > /dev/null 2>&1; then
        printf '      %b[FAIL] uv installed but not on PATH. Open a new terminal and re-run.%b\n' "$RED" "$NC"
        exit 1
    fi
    printf '      %buv installed: %s%b\n' "$GREEN" "$(command -v uv)" "$NC"
else
    printf '      %buv found: %s%b\n' "$GREEN" "$(command -v uv)" "$NC"
fi
printf '\n'

# ── [2/3] dartlens-mcp ─────────────────────────────
printf '%b[2/3] Installing dartlens-mcp...%b\n' "$CYAN" "$NC"

if ! uv tool install --force dartlens-mcp; then
    printf '      %b[FAIL] uv tool install failed.%b\n' "$RED" "$NC"
    exit 1
fi
printf '      %bdartlens-mcp installed via uv tool%b\n' "$GREEN" "$NC"
printf '\n'

case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) [ -d "$LOCAL_BIN" ] && PATH="$LOCAL_BIN:$PATH" && export PATH ;;
esac

# ── [3/3] MCP target config + DART API key ───────────────
# DARTLENS_TARGET 으로 등록 대상 지정 가능: claude-desktop / claude-code / both / auto
# 기본 auto: `claude` CLI 가 PATH 에 있으면 Claude Code, Desktop config 디렉토리 있으면 Desktop,
# 둘 다면 both, 아무것도 못 찾으면 claude-desktop.
DARTLENS_TARGET="${DARTLENS_TARGET:-auto}"
export DARTLENS_TARGET

printf '%b[3/3] Configuring MCP (target=%s, DART API key required)...%b\n' "$CYAN" "$DARTLENS_TARGET" "$NC"
printf '      DART API 키가 없다면 https://opendart.fss.or.kr 에서 발급 (분당 1,000건 / 일 20,000건)\n\n'

# arrays 대신 함수 + "$@" 으로 우회 (POSIX 호환).
# curl | sh 일 때 stdin은 파이프라 input() 이 막힌다. /dev/tty 가 있으면
# 거기에 연결해서 키 입력을 받게 한다. DART_API_KEY env 가 있으면 prompt 없이 진행.
run_setup() {
    if [ -x "$LOCAL_BIN/dartlens-setup" ]; then
        "$LOCAL_BIN/dartlens-setup" "$@"
    else
        uv tool run --from dartlens-mcp dartlens-setup "$@"
    fi
}

if [ -n "$DART_API_KEY" ] || [ ! -e /dev/tty ]; then
    run_setup
else
    run_setup < /dev/tty
fi

if [ $? -ne 0 ]; then
    printf '\n'
    printf '%b[FAIL] dartlens-setup failed. 키를 직접 다시 등록하려면:%b\n' "$RED" "$NC"
    printf '%b       dartlens-setup <YOUR_DART_API_KEY>%b\n' "$RED" "$NC"
    exit 1
fi
printf '\n'

# ── Verify ───────────────────────────────────────────────
printf '%bVerifying installation...%b\n' "$CYAN" "$NC"
run_doctor() {
    if [ -x "$LOCAL_BIN/dartlens-doctor" ]; then
        "$LOCAL_BIN/dartlens-doctor"
    else
        uv tool run --from dartlens-mcp dartlens-doctor
    fi
}
if ! run_doctor; then
    printf '\n'
    printf '%b[FAIL] Doctor reported critical issues. See above for fix commands.%b\n' "$RED" "$NC"
    exit 1
fi
printf '\n'

printf '==============================================\n'
printf '%b  Installation complete%b\n' "$GREEN" "$NC"
printf '==============================================\n\n'
printf 'Next steps:\n'
printf '  1. Fully quit Claude Desktop\n'
if [ "$OS" = "macOS" ]; then
    printf '     (Cmd+Q or menu bar -> Claude -> Quit)\n'
fi
printf '  2. Restart Claude Desktop\n'
printf '  3. Try: "삼성전자 최근 공시 보여줘"\n\n'
printf 'Update later:    uv tool upgrade dartlens-mcp\n'
printf 'Re-register key: dartlens-setup <KEY>\n'
printf 'Diagnose:        dartlens-doctor\n\n'
