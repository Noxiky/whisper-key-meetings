# Whisper Key Meetings - uninstaller
# Usage:
#   irm https://raw.githubusercontent.com/Noxiky/whisper-key-meetings/main/uninstall.ps1 | iex
#
# Or from the install dir:
#   .\uninstall.ps1
#
# Optional overrides:
#   $env:WKM_INSTALL_DIR    = "D:\apps\wkm"   # default: $env:USERPROFILE\whisper-key-meetings
#   $env:WKM_PURGE_CONFIG   = "1"             # ALSO remove %APPDATA%\whisperkey (settings + voice commands)
#   $env:WKM_PURGE_MODELS   = "1"             # ALSO remove ~\.cache\huggingface\hub (downloaded whisper models, can be many GB)
#   $env:WKM_YES            = "1"             # skip the confirmation prompt

$ErrorActionPreference = "Continue"

$INSTALL_DIR    = if ($env:WKM_INSTALL_DIR) { $env:WKM_INSTALL_DIR } else { Join-Path $env:USERPROFILE "whisper-key-meetings" }
$PURGE_CONFIG   = [bool]$env:WKM_PURGE_CONFIG
$PURGE_MODELS   = [bool]$env:WKM_PURGE_MODELS
$YES            = [bool]$env:WKM_YES

$configDir   = Join-Path $env:APPDATA "whisperkey"
$modelsCache = Join-Path $env:USERPROFILE ".cache\huggingface\hub"
$shortcut    = Join-Path ([Environment]::GetFolderPath('Desktop')) "Whisper Key Meetings.lnk"

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    [ok]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    [warn] $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  Whisper Key Meetings - uninstaller" -ForegroundColor White
Write-Host ""
Write-Host "Will remove:" -ForegroundColor White
Write-Host "  - $INSTALL_DIR"
Write-Host "  - $shortcut"
if ($PURGE_CONFIG) { Write-Host "  - $configDir  (user settings + voice commands)" -ForegroundColor Yellow }
else                { Write-Host "  - $configDir  (KEPT -- set `$env:WKM_PURGE_CONFIG='1' to remove)" -ForegroundColor DarkGray }
if ($PURGE_MODELS) { Write-Host "  - $modelsCache  (downloaded whisper models)" -ForegroundColor Yellow }
else                { Write-Host "  - $modelsCache  (KEPT -- set `$env:WKM_PURGE_MODELS='1' to remove)" -ForegroundColor DarkGray }
Write-Host ""

if (-not $YES) {
    $answer = Read-Host "Proceed? [y/N]"
    if ($answer -notmatch "^[Yy]") {
        Write-Host "Aborted." -ForegroundColor Yellow
        return
    }
}

Write-Step "Stopping any running Whisper Key processes"
$candidates = @("whisper-key", "python")
$killed = 0
foreach ($name in $candidates) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
        $path = $null
        try { $path = $_.Path } catch {}
        if ($path -and $path -like "$INSTALL_DIR\*") {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            $killed++
        }
    }
}
if ($killed -gt 0) { Write-OK "stopped $killed process(es)" } else { Write-OK "no running processes from install dir" }

Write-Step "Removing Desktop shortcut"
if (Test-Path $shortcut) {
    Remove-Item $shortcut -Force -ErrorAction SilentlyContinue
    Write-OK "removed $shortcut"
} else {
    Write-OK "no shortcut to remove"
}

Write-Step "Removing install directory"
if (Test-Path $INSTALL_DIR) {
    try {
        Remove-Item -Recurse -Force $INSTALL_DIR -ErrorAction Stop
        Write-OK "removed $INSTALL_DIR"
    } catch {
        Write-Warn "could not remove $INSTALL_DIR : $($_.Exception.Message)"
        Write-Warn "try closing any open file explorer windows / terminals in that path and re-run."
    }
} else {
    Write-OK "no install directory at $INSTALL_DIR"
}

if ($PURGE_CONFIG) {
    Write-Step "Removing user config"
    if (Test-Path $configDir) {
        Remove-Item -Recurse -Force $configDir -ErrorAction SilentlyContinue
        Write-OK "removed $configDir"
    } else {
        Write-OK "no config to remove"
    }
}

if ($PURGE_MODELS) {
    Write-Step "Removing downloaded whisper models"
    if (Test-Path $modelsCache) {
        Remove-Item -Recurse -Force $modelsCache -ErrorAction SilentlyContinue
        Write-OK "removed $modelsCache"
    } else {
        Write-OK "no model cache to remove"
    }
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Whisper Key Meetings uninstalled" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
