# Whisper Key Meetings - smoke test / health check
# Usage (preflight, before install):
#   irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/doctor.ps1 | iex
#
# Usage (post-install diagnostic, after the app is already installed):
#   cd $env:USERPROFILE\whisper-key-meetings
#   .\doctor.ps1
#
# Exit code 0 if all green, 1 if any check failed.

$ErrorActionPreference = "Continue"

$INSTALL_DIR = if ($env:WKM_INSTALL_DIR) { $env:WKM_INSTALL_DIR } else { Join-Path $env:USERPROFILE "whisper-key-meetings" }

Write-Host ""
Write-Host "  Whisper Key Meetings - doctor" -ForegroundColor White
Write-Host "  target install dir: $INSTALL_DIR" -ForegroundColor DarkGray
Write-Host ""

$script:results = @()

function Add-Result {
    param($Name, $Status, $Detail)
    $script:results += [pscustomobject]@{
        Name   = $Name
        Status = $Status
        Detail = $Detail
    }
}

# 1. PowerShell version
$psv = $PSVersionTable.PSVersion
if ($psv.Major -ge 5) {
    Add-Result "PowerShell" "pass" "$psv"
} else {
    Add-Result "PowerShell" "fail" "$psv -- need 5.0+"
}

# 2. git
if (Get-Command git -ErrorAction SilentlyContinue) {
    Add-Result "git" "pass" ((& git --version) -join " ")
} else {
    Add-Result "git" "fail" "not found -- install: winget install --id Git.Git -e"
}

# 3. Python 3.11-3.13
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
    $pyver = (& $pythonExe --version 2>&1) -join ""
    Add-Result "Python" "pass" "$pyver  ($pythonExe)"
} else {
    Add-Result "Python" "fail" "3.11-3.13 not found -- install: winget install --id Python.Python.3.12 -e"
}

# 4. Internet reachability (PyPI + GitHub raw)
foreach ($host_url in @("pypi.org", "raw.githubusercontent.com", "files.pythonhosted.org")) {
    try {
        $response = Invoke-WebRequest -Uri "https://$host_url" -Method Head -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
        Add-Result "internet -> $host_url" "pass" "HTTP $($response.StatusCode)"
    } catch {
        $msg = if ($_.Exception.Response) { "HTTP $($_.Exception.Response.StatusCode.value__)" } else { $_.Exception.Message }
        Add-Result "internet -> $host_url" "fail" $msg
    }
}

# 5. Disk space (need ~3 GB free on the USERPROFILE drive)
try {
    $drive = (Get-Item $env:USERPROFILE).PSDrive
    $freeGB = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGB -ge 3) {
        Add-Result "disk space" "pass" "$freeGB GB free on $($drive.Name):"
    } elseif ($freeGB -ge 1.5) {
        Add-Result "disk space" "warn" "$freeGB GB free -- recommend 3+ GB"
    } else {
        Add-Result "disk space" "fail" "$freeGB GB free -- need at least 3 GB"
    }
} catch {
    Add-Result "disk space" "warn" "could not measure: $($_.Exception.Message)"
}

# 6. NVIDIA GPU (informational - changes install size)
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    try {
        $gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null) | Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -and $gpu) {
            Add-Result "NVIDIA GPU" "pass" "$($gpu.Trim()) -- CUDA libs will be installed (~500 MB extra)"
        } else {
            Add-Result "NVIDIA GPU" "warn" "nvidia-smi present but no GPU returned -- CPU mode"
        }
    } catch {
        Add-Result "NVIDIA GPU" "warn" "nvidia-smi error -- CPU mode"
    }
} else {
    Add-Result "NVIDIA GPU" "warn" "not detected -- CPU mode (slower transcription)"
}

# 7. If install dir already exists, verify it
if (Test-Path $INSTALL_DIR) {
    $venvPython = Join-Path $INSTALL_DIR ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        Add-Result "existing venv" "pass" $venvPython

        try {
            $check = & $venvPython -c "import faster_whisper, sounddevice, soundcard; print('ok')" 2>&1 | Out-String
            if ($check -match "ok") {
                Add-Result "core packages" "pass" "faster_whisper / sounddevice / soundcard import OK"
            } else {
                Add-Result "core packages" "fail" $check.Trim()
            }
        } catch {
            Add-Result "core packages" "fail" $_.Exception.Message
        }

        # CUDA DLL check only if NVIDIA was detected
        $cublasDll = Join-Path $INSTALL_DIR ".venv\Lib\site-packages\nvidia\cublas\bin\cublas64_12.dll"
        $cudnnDir  = Join-Path $INSTALL_DIR ".venv\Lib\site-packages\nvidia\cudnn\bin"
        if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
            if (Test-Path $cublasDll) {
                Add-Result "CUDA cublas DLL" "pass" "$cublasDll"
            } else {
                Add-Result "CUDA cublas DLL" "fail" "missing -- run: .venv\Scripts\python.exe -m pip install nvidia-cublas-cu12"
            }
            if (Test-Path $cudnnDir) {
                Add-Result "CUDA cudnn DLLs" "pass" "$cudnnDir"
            } else {
                Add-Result "CUDA cudnn DLLs" "fail" "missing -- run: .venv\Scripts\python.exe -m pip install nvidia-cudnn-cu12"
            }
        }
    } else {
        Add-Result "existing venv" "fail" "$INSTALL_DIR exists but .venv\Scripts\python.exe missing -- rerun installer"
    }
}

# Render results
Write-Host ""
$fails = 0
$warns = 0
foreach ($r in $script:results) {
    $marker = "    [ok] "
    $color  = "Green"
    if ($r.Status -eq "warn") { $marker = "    [!!] "; $color = "Yellow"; $warns++ }
    if ($r.Status -eq "fail") { $marker = "    [XX] "; $color = "Red"; $fails++ }
    Write-Host "$marker$($r.Name): $($r.Detail)" -ForegroundColor $color
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor DarkGray
if ($fails -eq 0 -and $warns -eq 0) {
    Write-Host "  All green. Safe to install." -ForegroundColor Green
    Write-Host "================================================================" -ForegroundColor DarkGray
    exit 0
} elseif ($fails -eq 0) {
    Write-Host "  $warns warning(s). Install will proceed but check the [!!] lines above." -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor DarkGray
    exit 0
} else {
    Write-Host "  $fails failure(s), $warns warning(s). Fix the [XX] lines before installing." -ForegroundColor Red
    Write-Host "================================================================" -ForegroundColor DarkGray
    exit 1
}
