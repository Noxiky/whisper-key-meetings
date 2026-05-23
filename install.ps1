# Whisper Key Meetings - one-paste installer for Windows
# Usage:
#   irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/install.ps1 | iex
#
# For deeper diagnostics, run:
#   irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/doctor.ps1 | iex
#
# Optional overrides (set BEFORE the irm | iex line):
#   $env:WKM_INSTALL_DIR = "D:\apps\wkm"    # default: $env:USERPROFILE\whisper-key-meetings
#   $env:WKM_NO_LAUNCH   = "1"              # skip the final "launch now" prompt
#   $env:WKM_BRANCH      = "main"           # branch / tag to clone
#   $env:WKM_NO_PREFLIGHT = "1"             # skip preflight checks (not recommended)

$ErrorActionPreference = "Stop"

$REPO_URL       = "https://github.com/Noxiky/whisper-key-meetings.git"
$INSTALL_DIR    = if ($env:WKM_INSTALL_DIR) { $env:WKM_INSTALL_DIR } else { Join-Path $env:USERPROFILE "whisper-key-meetings" }
$BRANCH         = if ($env:WKM_BRANCH) { $env:WKM_BRANCH } else { "main" }
$NO_LAUNCH      = [bool]$env:WKM_NO_LAUNCH
$NO_PREFLIGHT   = [bool]$env:WKM_NO_PREFLIGHT

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "    [err]  $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "  Whisper Key Meetings - installer" -ForegroundColor White
Write-Host "  https://github.com/Noxiky/whisper-key-meetings" -ForegroundColor DarkGray
Write-Host "  target: $INSTALL_DIR  (branch: $BRANCH)" -ForegroundColor DarkGray

# ----- preflight checks ---------------------------------------------------

if (-not $NO_PREFLIGHT) {
    Write-Step "Preflight checks (set `$env:WKM_NO_PREFLIGHT='1' to skip)"
    $preflightFail = $false

    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-OK ("git: " + ((git --version) -join ' '))
    } else {
        Write-Err "git not found. Install: winget install --id Git.Git -e"
        $preflightFail = $true
    }

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
    if ($pythonExe) {
        Write-OK ("python: " + ((& $pythonExe --version) -join " ") + "  ($pythonExe)")
    } else {
        Write-Err "Python 3.11-3.13 not found. Install: winget install --id Python.Python.3.12 -e"
        $preflightFail = $true
    }

    try {
        $null = Invoke-WebRequest -Uri "https://pypi.org" -Method Head -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
        Write-OK "internet: pypi.org reachable"
    } catch {
        Write-Err "pypi.org not reachable: $($_.Exception.Message)"
        $preflightFail = $true
    }

    try {
        $drive = (Get-Item $env:USERPROFILE).PSDrive
        $freeGB = [math]::Round($drive.Free / 1GB, 1)
        if ($freeGB -ge 1.5) {
            Write-OK "disk: $freeGB GB free on $($drive.Name):"
        } else {
            Write-Err "disk: only $freeGB GB free on $($drive.Name): -- need >= 1.5 GB"
            $preflightFail = $true
        }
    } catch {
        Write-Warn "disk: could not measure free space"
    }

    if ($preflightFail) {
        Write-Host ""
        Write-Host "  Preflight failed. Fix the items above and re-run." -ForegroundColor Red
        Write-Host "  Full diagnostic: irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/doctor.ps1 | iex" -ForegroundColor DarkGray
        Write-Host ""
        return
    }
} else {
    $pythonExe = $null
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.13", "3.12", "3.11")) {
            try {
                $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $exe) { $pythonExe = $exe.Trim(); break }
            } catch {}
        }
    }
    if (-not $pythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $pythonExe = (Get-Command python).Source
    }
}

# ----- fetch source -------------------------------------------------------

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

# ----- venv ---------------------------------------------------------------

Write-Step "Creating Python virtual environment"
$venvPython = Join-Path $INSTALL_DIR ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    & $pythonExe -m venv .venv
}
Write-OK ".venv ready"

# ----- core deps ----------------------------------------------------------

Write-Step "Installing core dependencies (faster-whisper, sounddevice, soundcard, ...)"
Write-Host "    pip progress follows. First-time install takes 3-8 minutes." -ForegroundColor DarkGray
& $venvPython -m pip install --upgrade pip --disable-pip-version-check
& $venvPython -m pip install -e . --disable-pip-version-check --timeout 60 --retries 3
if ($LASTEXITCODE -ne 0) {
    Write-Err "Core dependency install failed. Check the pip output above."
    return
}
Write-OK "core dependencies installed"

# ----- CUDA libs (per-package, resilient) ---------------------------------

function Install-PipPackage-Resilient {
    param(
        [string]$PythonExe,
        [string]$Package,
        [int]$TimeoutSec = 60,
        [int]$MaxAttempts = 3
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        Write-Host "    -> $Package (attempt $attempt/$MaxAttempts)" -ForegroundColor DarkGray
        & $PythonExe -m pip install --disable-pip-version-check --timeout $TimeoutSec --retries 2 $Package
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
        if ($attempt -lt $MaxAttempts) {
            Write-Warn "$Package failed, retrying in 5s..."
            Start-Sleep -Seconds 5
        }
    }
    return $false
}

function Test-NvidiaGpuPresent {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return $false }
    try {
        $null = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (Test-NvidiaGpuPresent) {
    Write-Step "NVIDIA GPU detected -- installing CUDA 12 runtime libraries (~500 MB)"
    $cudaPkgs = @("nvidia-cuda-runtime-cu12", "nvidia-cudnn-cu12", "nvidia-cublas-cu12")
    $failed = @()
    foreach ($pkg in $cudaPkgs) {
        if (-not (Install-PipPackage-Resilient -PythonExe $venvPython -Package $pkg)) {
            $failed += $pkg
        }
    }
    if ($failed.Count -eq 0) {
        Write-OK "CUDA libraries installed"
    } else {
        Write-Warn "Some CUDA packages failed: $($failed -join ', ')"
        Write-Warn "Whisper will fall back to CPU. To retry later:"
        Write-Warn "  cd $INSTALL_DIR; .venv\Scripts\python.exe -m pip install --verbose $($failed -join ' ')"
    }
} else {
    Write-OK "no NVIDIA GPU detected -- skipping CUDA libraries (CPU mode)"
}

# ----- post-install verification ------------------------------------------

Write-Step "Verifying install"
$verifyOk = $true

try {
    & $venvPython -c "import faster_whisper, sounddevice, soundcard" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "imports: faster_whisper / sounddevice / soundcard"
    } else {
        Write-Err "core imports failed"
        $verifyOk = $false
    }
} catch {
    Write-Err "core imports failed: $($_.Exception.Message)"
    $verifyOk = $false
}

if (Test-NvidiaGpuPresent) {
    $cublasDll = Join-Path $INSTALL_DIR ".venv\Lib\site-packages\nvidia\cublas\bin\cublas64_12.dll"
    if (Test-Path $cublasDll) {
        Write-OK "CUDA: cublas64_12.dll present"
    } else {
        Write-Warn "CUDA: cublas64_12.dll NOT present -- GPU mode will error. Re-run the CUDA install or it falls back to CPU."
        $verifyOk = $false
    }

    Write-Host "    running GPU load test (downloads ~75 MB tiny model, ~30s)..." -ForegroundColor DarkGray
    $gpuTest = @"
import sys, numpy as np
from faster_whisper import WhisperModel
m = WhisperModel('tiny', device='cuda', compute_type='float16')
segments, _ = m.transcribe(np.zeros(16000, dtype=np.float32))
list(segments)
print('gpu_load_ok')
"@
    $gpuOut = & $venvPython -c $gpuTest 2>&1 | Out-String
    if ($gpuOut -match "gpu_load_ok") {
        Write-OK "GPU: tiny model loaded and transcribed on CUDA"
    } else {
        Write-Warn "GPU load test failed -- the app will fall back to CPU when needed."
        $firstErr = ($gpuOut -split "`n" | Where-Object { $_ -match "Error|error|Exception" } | Select-Object -First 1)
        if ($firstErr) { Write-Warn ("error: " + $firstErr.Trim()) }
        $verifyOk = $false
    }
}

# ----- desktop shortcut ---------------------------------------------------

Write-Step "Creating Desktop shortcut"
try {
    $launcher = Join-Path $INSTALL_DIR "run-whisper-key.cmd"
    $iconPath = Join-Path $INSTALL_DIR "src\whisper_key\platform\windows\assets\whisperkey-icon.ico"
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) "Whisper Key Meetings.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $INSTALL_DIR
    $shortcut.Description = "Whisper Key Meetings - local speech-to-text + live meeting transcription"
    if (Test-Path $iconPath) { $shortcut.IconLocation = $iconPath }
    $shortcut.Save()
    Write-OK "shortcut: $shortcutPath"
} catch {
    Write-Warn "Could not create Desktop shortcut: $_"
}

# ----- final report -------------------------------------------------------

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
if ($verifyOk) {
    Write-Host "  Whisper Key Meetings installed and verified" -ForegroundColor Green
} else {
    Write-Host "  Whisper Key Meetings installed WITH WARNINGS (see [warn] above)" -ForegroundColor Yellow
}
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Location:   $INSTALL_DIR" -ForegroundColor White
Write-Host "Launch:     cd $INSTALL_DIR; .\run-whisper-key.cmd  (or double-click the Desktop shortcut)" -ForegroundColor White
Write-Host ""
Write-Host "Hotkeys:" -ForegroundColor White
Write-Host "  Ctrl+Win  -> dictation toggle (start/stop)"
Write-Host "  Alt+Win   -> voice commands"
Write-Host "  F9        -> meeting listener  ([MIC] + [SYS] live transcription)"
Write-Host ""
Write-Host "If anything looks off, run the diagnostic:" -ForegroundColor DarkGray
Write-Host "  irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/doctor.ps1 | iex" -ForegroundColor DarkGray
Write-Host ""

if (-not $NO_LAUNCH) {
    $answer = Read-Host "Launch Whisper Key now? [Y/n]"
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        Start-Process -FilePath (Join-Path $INSTALL_DIR "run-whisper-key.cmd") -WorkingDirectory $INSTALL_DIR
    }
}
