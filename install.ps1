# dartlens Installer (Windows · PowerShell)
# Usage:
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.ps1 | iex"
#
# 3 steps:
#   1) uv (Python package manager)  — auto-installs Python runtime if missing
#   2) dartlens-mcp via `uv tool install`
#   3) Claude Desktop config + DART API key validation via `dartlens-setup`

$ErrorActionPreference = 'Stop'

$ESC = [char]27
function Info($msg)  { Write-Host "$ESC[36m$msg$ESC[0m" }
function OK($msg)    { Write-Host "$ESC[32m$msg$ESC[0m" }
function Warn($msg)  { Write-Host "$ESC[33m$msg$ESC[0m" }
function Err($msg)   { Write-Host "$ESC[31m$msg$ESC[0m" }

Write-Host ""
Write-Host "=============================================="
Write-Host "  dartlens Installer (Windows)"
Write-Host "=============================================="
Write-Host ""

$LocalBin = Join-Path $env:USERPROFILE ".local\bin"

# ── [1/3] uv ─────────────────────────────────────────────
Info "[1/4] Checking uv..."
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Info "      uv not found. Installing from astral.sh..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Err "      [FAIL] uv installation failed: $_"
        Err "      Manual install: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    }
    if (Test-Path $LocalBin) {
        $env:Path = "$LocalBin;$env:Path"
    }
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Err "      [FAIL] uv installed but not on PATH. Open a new PowerShell and re-run."
        exit 1
    }
    OK "      uv installed: $($uvCmd.Source)"
} else {
    OK "      uv found: $($uvCmd.Source)"
}
Write-Host ""

# ── [2/3] dartlens-mcp ─────────────────────────────
Info "[2/4] Installing dartlens-mcp..."

# --force re-creates the tool environment, so re-running upgrades cleanly.
& uv tool install --force dartlens-mcp
if ($LASTEXITCODE -ne 0) {
    Err "      [FAIL] uv tool install failed."
    exit 1
}
OK "      dartlens-mcp installed via uv tool"
Write-Host ""

# Ensure .local\bin is on PATH for the rest of this session
if (Test-Path $LocalBin -PathType Container) {
    if (-not ($env:Path -split ';' -contains $LocalBin)) {
        $env:Path = "$LocalBin;$env:Path"
    }
}

# ── [3/3] MCP target config + DART API key ───────────────
# $env:DARTLENS_TARGET 으로 등록 대상 지정: claude-desktop / claude-code / both / auto
if (-not $env:DARTLENS_TARGET) { $env:DARTLENS_TARGET = "auto" }

Info "[3/4] Configuring MCP (target=$($env:DARTLENS_TARGET), DART API key required)..."
Write-Host "      DART API 키가 없다면 https://opendart.fss.or.kr 에서 발급 (분당 1,000건 / 일 20,000건)"
Write-Host ""

$setupExe = Join-Path $LocalBin "dartlens-setup.exe"

# If DART_API_KEY is already set, setup can run without prompting.
if (Test-Path $setupExe) {
    & $setupExe
} else {
    & uv tool run --from dartlens-mcp dartlens-setup
}
if ($LASTEXITCODE -ne 0) {
    Err ""
    Err "[FAIL] dartlens-setup failed. 키를 직접 다시 등록하려면:"
    Err "       dartlens-setup <YOUR_DART_API_KEY>"
    exit 1
}
Write-Host ""

# ── [4/4] License activation ─────────────────────────────
Info "[4/4] License activation..."
Write-Host "      Enter the license key sent to your email after purchase."
$activateExe = Join-Path $LocalBin "dartlens-activate.exe"
$licKey = (Read-Host "      License key").Trim()
if ($licKey) {
    if (Test-Path $activateExe) {
        & $activateExe $licKey
    } else {
        & uv tool run --from dartlens-mcp dartlens-activate $licKey
    }
    if ($LASTEXITCODE -ne 0) {
        Warn "      Activation failed. Check the key and retry: dartlens-activate <LICENSE-KEY>"
    }
} else {
    Warn "      Skipped. Activate later: dartlens-activate <LICENSE-KEY>"
}
Write-Host ""

# ── Verify ───────────────────────────────────────────────
Info "Verifying installation..."
$doctorExe = Join-Path $LocalBin "dartlens-doctor.exe"
if (Test-Path $doctorExe) {
    & $doctorExe
} else {
    & uv tool run --from dartlens-mcp dartlens-doctor
}
if ($LASTEXITCODE -ne 0) {
    Err ""
    Err "[FAIL] Doctor reported critical issues. See above for fix commands."
    exit 1
}
Write-Host ""

Write-Host "=============================================="
OK     "  Installation complete"
Write-Host "=============================================="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. FULLY quit Claude Desktop (tray icon -> Quit)"
Write-Host "  2. Restart Claude Desktop"
Write-Host "  3. Try: '삼성전자 최근 공시 보여줘'"
Write-Host ""
Write-Host "Activate:        dartlens-activate <LICENSE-KEY>"
Write-Host "Update later:    uv tool upgrade dartlens-mcp"
Write-Host "Re-register key: dartlens-setup <KEY>"
Write-Host "Diagnose:        dartlens-doctor"
Write-Host ""
