[CmdletBinding()]
param(
    [string]$IsccPath = "",
    [switch]$SkipDistributionVerification,
    [switch]$SkipCompile
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$distribution = Join-Path $projectRoot "dist\WhisperKey"
$releaseDir = Join-Path $projectRoot "artifacts\release"
$manifest = Join-Path $releaseDir "release-manifest.json"
$installerScript = Join-Path $projectRoot "installer\WhisperKey.iss"
$licenseIndex = Join-Path $releaseDir "THIRD_PARTY_LICENSES\INDEX.json"
$offlineEvidence = Join-Path $releaseDir "OFFLINE_STARTUP_EVIDENCE.json"
$sbom = Join-Path $releaseDir "sbom.cdx.json"
$privacyNotice = Join-Path $projectRoot "src\whisper_key\assets\PRIVACY_RECORDING_NOTICE.md"
$pyproject = Get-Content -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($pyproject, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "Could not read the project version from pyproject.toml"
}
$version = $versionMatch.Groups[1].Value
if (-not (Test-Path -LiteralPath $licenseIndex)) {
    throw "Third-party license index is missing: $licenseIndex"
}
if (-not (Test-Path -LiteralPath $offlineEvidence)) {
    throw "Offline startup evidence is missing: $offlineEvidence"
}
if (-not (Test-Path -LiteralPath $sbom)) {
    throw "CycloneDX SBOM is missing: $sbom"
}
if (-not (Test-Path -LiteralPath $privacyNotice)) {
    throw "Privacy/recording notice is missing: $privacyNotice"
}
$offlineData = Get-Content -LiteralPath $offlineEvidence -Raw | ConvertFrom-Json
$distributionExecutable = Join-Path $distribution "WhisperKey.exe"
$distributionExecutableHash = (Get-FileHash -LiteralPath $distributionExecutable -Algorithm SHA256).Hash
if (-not $offlineData.passed -or $offlineData.exit_code -ne 0 -or $offlineData.stderr_bytes -ne 0) {
    throw "Offline startup evidence does not record a clean pass"
}
if ($offlineData.executable_sha256 -ne $distributionExecutableHash) {
    throw "Offline startup evidence belongs to a different executable"
}

if (-not $SkipDistributionVerification) {
    & python (Join-Path $PSScriptRoot "verify_release_install.py") $distribution $manifest
    if ($LASTEXITCODE -ne 0) {
        throw "The packaged distribution does not match its release manifest"
    }
}

if (-not $SkipCompile) {
    if (-not $IsccPath) {
        $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
        if ($command) {
            $IsccPath = $command.Source
        } else {
            $knownPaths = @(
                (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
                "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                "C:\Program Files\Inno Setup 6\ISCC.exe"
            )
            $IsccPath = $knownPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        }
    }
    if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) {
        throw "ISCC.exe was not found. Install Inno Setup 6 or pass -IsccPath."
    }
}

$manifestData = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
if ($manifestData.version -ne $version) {
    throw "Release manifest version $($manifestData.version) does not match project version $version"
}
$releaseManifestHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
$sbomData = Get-Content -LiteralPath $sbom -Raw | ConvertFrom-Json
if ($sbomData.bomFormat -ne "CycloneDX" -or $sbomData.specVersion -ne "1.7") {
    throw "SBOM is not a CycloneDX 1.7 document"
}
if ($sbomData.components.Count -lt 1) {
    throw "SBOM component inventory is empty"
}
$sbomBinding = $sbomData.metadata.component.properties |
    Where-Object { $_.name -eq "whisperkey:release-manifest-sha256" } |
    Select-Object -First 1
if (-not $sbomBinding -or $sbomBinding.value -ne $releaseManifestHash) {
    throw "SBOM is stale or belongs to a different release manifest"
}

if (-not $SkipCompile) {
    & $IsccPath "/DMyAppVersion=$version" $installerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed with exit code $LASTEXITCODE"
    }
}

$setupPath = Join-Path $releaseDir "WhisperKey-$version-windows-x64-setup.exe"
if (-not (Test-Path -LiteralPath $setupPath)) {
    throw "Expected installer was not produced: $setupPath"
}
$setup = Get-Item -LiteralPath $setupPath
$setupHash = Get-FileHash -LiteralPath $setupPath -Algorithm SHA256
$sbomHash = Get-FileHash -LiteralPath $sbom -Algorithm SHA256
$privacyNoticeHash = Get-FileHash -LiteralPath $privacyNotice -Algorithm SHA256
$signature = Get-AuthenticodeSignature -LiteralPath $setupPath
$installerEvidence = [ordered]@{
    product = "WhisperKey"
    version = $version
    platform = "windows-x64"
    created_utc = [DateTime]::UtcNow.ToString("o")
    installer = $setup.Name
    bytes = $setup.Length
    sha256 = $setupHash.Hash.ToLowerInvariant()
    signature_status = $signature.Status.ToString()
    release_manifest = (Split-Path -Leaf $manifest)
    release_manifest_sha256 = $releaseManifestHash
    sbom = (Split-Path -Leaf $sbom)
    sbom_sha256 = $sbomHash.Hash.ToLowerInvariant()
    privacy_notice = (Split-Path -Leaf $privacyNotice)
    privacy_notice_sha256 = $privacyNoticeHash.Hash.ToLowerInvariant()
}
$installerManifest = Join-Path $releaseDir "installer-manifest.json"
$installerEvidence | ConvertTo-Json | Set-Content -LiteralPath $installerManifest -Encoding UTF8
[pscustomobject]@{
    Installer = $setup.FullName
    Version = $version
    Bytes = $setup.Length
    SHA256 = $setupHash.Hash
    Signature = $signature.Status
    Evidence = $installerManifest
} | Format-List
