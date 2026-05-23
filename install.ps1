# Whisper Key Meetings - one-paste installer for Windows
# Usage:
#   irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/install.ps1 | iex
#
# Optional overrides (set BEFORE the irm | iex line):
#   $env:WKM_INSTALL_DIR = "D:\apps\wkm"   # default: $env:USERPROFILE\whisper-key-meetings
#   $env:WKM_NO_LAUNCH   = "1"             # skip the final "launch now" prompt
#   $env:WKM_BRANCH      = "main"          # branch / tag to clone

$ErrorActionPreference = "Stop"

$REPO_URL    = "https://github.com/Noxiky/whisper-key-meetings.git"
$INSTALL_DIR = if ($env:WKM_INSTALL_DIR) { $env:WKM_INSTALL_DIR } else { Join-Path $env:USERPROFILE "whisper-key-meetings" }
$BRANCH      = if ($env:WKM_BRANCH) { $env:WKM_BRANCH } else { "main" }
$NO_LAUNCH   = [bool]$env:WKM_NO_LAUNCH

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "    [err]  $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "  Whisper Key Meetings - installer" -ForegroundColor White
Write-Host "  https://github.com/Noxiky/whisper-key-meetings" -ForegroundColor DarkGray
Write-Host "  target: $INSTALL_DIR  (branch: $BRANCH)" -ForegroundColor DarkGray

Write-Step "Checking prerequisites"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Err "git not found. Install it with:  winget install --id Git.Git -e"
    Write-Err "then re-run this installer."
    return
}
Write-OK ("git: " + ((git --version) -join ' '))

function Resolve-PythonExe {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.13", "3.12", "3.11")) {
            try {
                $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) {
                    return $exe.Trim()
                }
            } catch {}
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            $version = & python --version 2>&1 | Out-String
            if ($version -match "Python 3\.1[1-3]") {
                return (Get-Command python).Source
            }
        } catch {}
    }
    return $null
}

$pythonExe = Resolve-PythonExe
if (-not $pythonExe) {
    Write-Err "Python 3.11-3.13 not found. Install it with:  winget install --id Python.Python.3.12 -e"
    Write-Err "then re-run this installer."
    return
}
Write-OK ("python: " + (& $pythonExe --version) + "  (" + $pythonExe + ")")

Write-Step "Fetching source"
if (Test-Path $INSTALL_DIR) {
    Write-Warn "Directory exists - pulling latest"
    Push-Location $INSTALL_DIR
    try {
        & git fetch origin $BRANCH --depth 1
        & git checkout $BRANCH
        & git reset --hard "origin/$BRANCH"
        Write-OK "updated to origin/$BRANCH"
    } finally {
        Pop-Location
    }
} else {
    & git clone --branch $BRANCH --depth 1 $REPO_URL $INSTALL_DIR
    Write-OK "cloned"
}

Set-Location $INSTALL_DIR

Write-Step "Creating Python virtual environment"
$venvPython = Join-Path $INSTALL_DIR ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $pythonExe -m venv .venv
}
Write-OK ".venv ready at " + (Join-Path $INSTALL_DIR ".venv")

Write-Step "Installing dependencies (takes 2-5 minutes the first time)"
& $venvPython -m pip install --upgrade pip --disable-pip-version-check --quiet
& $venvPython -m pip install -e . --disable-pip-version-check --quiet
Write-OK "dependencies installed"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Whisper Key Meetings installed" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Location:   $INSTALL_DIR" -ForegroundColor White
Write-Host "Launch:     cd $INSTALL_DIR; .\run-whisper-key.cmd" -ForegroundColor White
Write-Host ""
Write-Host "Hotkeys:" -ForegroundColor White
Write-Host "  Ctrl+Win  -> dictation (transcribe to cursor)"
Write-Host "  Alt+Win   -> voice commands"
Write-Host "  F9        -> meeting listener  ([MIC] + [SYS] live transcription)"
Write-Host ""
Write-Host "On first launch the app may offer GPU acceleration setup." -ForegroundColor DarkGray
Write-Host ""

if (-not $NO_LAUNCH) {
    $answer = Read-Host "Launch Whisper Key now? [Y/n]"
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        Start-Process -FilePath (Join-Path $INSTALL_DIR "run-whisper-key.cmd") -WorkingDirectory $INSTALL_DIR
    }
}
