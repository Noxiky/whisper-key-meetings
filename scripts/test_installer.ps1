[CmdletBinding()]
param(
    [string]$Installer = "",
    [switch]$RunStartupTest
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $projectRoot "artifacts\release"
$manifest = Join-Path $releaseDir "release-manifest.json"
$pyproject = Get-Content -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($pyproject, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "Could not read the project version from pyproject.toml"
}
$version = $versionMatch.Groups[1].Value
if (-not $Installer) {
    $Installer = Join-Path $releaseDir "WhisperKey-$version-windows-x64-setup.exe"
}
$Installer = (Resolve-Path -LiteralPath $Installer).Path

$testRoot = Join-Path $projectRoot ("artifacts\installer-test\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$installDir = Join-Path $testRoot "installed\WhisperKey"
$firstLog = Join-Path $testRoot "install-first.log"
$updateLog = Join-Path $testRoot "install-update.log"
$uninstallLog = Join-Path $testRoot "uninstall.log"
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

function Invoke-Setup([string]$LogPath) {
    $arguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/NOICONS",
        "/DIR=$installDir",
        "/LOG=$LogPath"
    )
    $process = Start-Process -FilePath $Installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Installer failed with exit code $($process.ExitCode). See $LogPath"
    }
}

function Assert-InstalledTree {
    $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
    & $python (Join-Path $PSScriptRoot "verify_release_install.py") $installDir $manifest --allow-extra
    if ($LASTEXITCODE -ne 0) {
        throw "Installed tree does not match the release manifest"
    }
    foreach ($required in @(
        "LICENSE-WhisperKey.txt",
        "THIRD_PARTY_NOTICES.txt",
        "THIRD_PARTY_LICENSES\INDEX.json",
        "PRIVACY_RECORDING_NOTICE.md",
        "sbom.cdx.json",
        "OFFLINE_STARTUP_EVIDENCE.json",
        "release-manifest.json",
        "SHA256SUMS.txt",
        "unins000.exe"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $installDir $required))) {
            throw "Installer did not include required file: $required"
        }
    }
    $offlineEvidence = Get-Content -LiteralPath (Join-Path $installDir "OFFLINE_STARTUP_EVIDENCE.json") -Raw | ConvertFrom-Json
    $installedExecutableHash = (Get-FileHash -LiteralPath (Join-Path $installDir "WhisperKey.exe") -Algorithm SHA256).Hash
    if (-not $offlineEvidence.passed -or $offlineEvidence.executable_sha256 -ne $installedExecutableHash) {
        throw "Installed offline evidence is stale or does not record a pass"
    }
    $licenseIndex = Get-Content -LiteralPath (Join-Path $installDir "THIRD_PARTY_LICENSES\INDEX.json") -Raw | ConvertFrom-Json
    if ($licenseIndex.distributions.Count -lt 1) {
        throw "Installed third-party license index is empty"
    }
    $sbom = Get-Content -LiteralPath (Join-Path $installDir "sbom.cdx.json") -Raw | ConvertFrom-Json
    if ($sbom.bomFormat -ne "CycloneDX" -or $sbom.specVersion -ne "1.7" -or $sbom.components.Count -lt 1) {
        throw "Installed SBOM is invalid or empty"
    }
    $notice = Get-Item -LiteralPath (Join-Path $installDir "PRIVACY_RECORDING_NOTICE.md")
    if ($notice.Length -lt 500) {
        throw "Installed privacy/recording notice is unexpectedly empty"
    }
}

Invoke-Setup $firstLog
Assert-InstalledTree

# A second pass against the same AppId and directory exercises the update/repair path.
Invoke-Setup $updateLog
Assert-InstalledTree

if ($RunStartupTest) {
    if (Get-Process -Name "WhisperKey" -ErrorAction SilentlyContinue) {
        throw "Close the running WhisperKey instance before using -RunStartupTest"
    }
    $executable = Join-Path $installDir "WhisperKey.exe"
    $previousOffline = $env:HF_HUB_OFFLINE
    $previousTelemetry = $env:HF_HUB_DISABLE_TELEMETRY
    try {
        $env:HF_HUB_OFFLINE = "1"
        $env:HF_HUB_DISABLE_TELEMETRY = "1"
        $startup = Start-Process -FilePath $executable -ArgumentList "--startup-test" -Wait -PassThru
    }
    finally {
        if ($null -eq $previousOffline) { Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue }
        else { $env:HF_HUB_OFFLINE = $previousOffline }
        if ($null -eq $previousTelemetry) { Remove-Item Env:HF_HUB_DISABLE_TELEMETRY -ErrorAction SilentlyContinue }
        else { $env:HF_HUB_DISABLE_TELEMETRY = $previousTelemetry }
    }
    if ($startup.ExitCode -ne 0) {
        throw "Installed startup test failed with exit code $($startup.ExitCode)"
    }
}

$uninstaller = Join-Path $installDir "unins000.exe"
$uninstall = Start-Process -FilePath $uninstaller -ArgumentList @(
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/LOG=$uninstallLog"
) -Wait -PassThru -WindowStyle Hidden
if ($uninstall.ExitCode -ne 0) {
    throw "Uninstaller failed with exit code $($uninstall.ExitCode). See $uninstallLog"
}
if (Test-Path -LiteralPath (Join-Path $installDir "WhisperKey.exe")) {
    throw "Uninstall left the application executable behind"
}
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{2470E795-33BC-45B5-9D78-9C05014FB825}_is1"
if (Test-Path -LiteralPath $uninstallKey) {
    throw "Uninstall registration remains after uninstall"
}

[pscustomobject]@{
    Installer = $Installer
    Version = $version
    TestRoot = $testRoot
    FirstInstall = "pass"
    UpdateRepair = "pass"
    InstalledStartup = if ($RunStartupTest) { "pass" } else { "not requested" }
    Uninstall = "pass"
} | Format-List
