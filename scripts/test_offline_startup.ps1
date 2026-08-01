[CmdletBinding()]
param(
    [string]$Executable = "",
    [string]$Output = "",
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Executable) {
    $Executable = Join-Path $projectRoot "dist\WhisperKey\WhisperKey.exe"
}
if (-not $Output) {
    $Output = Join-Path $projectRoot "artifacts\release\OFFLINE_STARTUP_EVIDENCE.json"
}
$Executable = (Resolve-Path -LiteralPath $Executable).Path
$outputFolder = Split-Path -Parent $Output
New-Item -ItemType Directory -Path $outputFolder -Force | Out-Null
$stdout = Join-Path $outputFolder "offline-startup.stdout.log"
$stderr = Join-Path $outputFolder "offline-startup.stderr.log"
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

if (Get-Process -Name "WhisperKey" -ErrorAction SilentlyContinue) {
    throw "Close the running WhisperKey instance before testing offline startup"
}

$previousOffline = $env:HF_HUB_OFFLINE
$previousTelemetry = $env:HF_HUB_DISABLE_TELEMETRY
$started = [DateTime]::UtcNow
$watch = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $env:HF_HUB_OFFLINE = "1"
    $env:HF_HUB_DISABLE_TELEMETRY = "1"
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.Arguments = "--startup-test"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Windows did not start the packaged executable" }
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Offline startup exceeded $TimeoutSeconds seconds"
    }
    $process.WaitForExit()
    $stdoutText = $process.StandardOutput.ReadToEnd()
    $stderrText = $process.StandardError.ReadToEnd()
    [System.IO.File]::WriteAllText($stdout, $stdoutText, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($stderr, $stderrText, [System.Text.UTF8Encoding]::new($false))
    $watch.Stop()
    $stderrBytes = (Get-Item -LiteralPath $stderr).Length
    $exitCode = $process.ExitCode
    $passed = $exitCode -eq 0 -and $stderrBytes -eq 0
    $evidence = [ordered]@{
        product = "WhisperKey"
        check = "cached-model-offline-startup"
        created_utc = [DateTime]::UtcNow.ToString("o")
        started_utc = $started.ToString("o")
        executable = Split-Path -Leaf $Executable
        executable_sha256 = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant()
        offline_environment = [ordered]@{
            HF_HUB_OFFLINE = "1"
            HF_HUB_DISABLE_TELEMETRY = "1"
        }
        elapsed_seconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
        exit_code = $exitCode
        stdout_bytes = (Get-Item -LiteralPath $stdout).Length
        stderr_bytes = $stderrBytes
        passed = $passed
    }
    $evidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Output -Encoding UTF8
    if (-not $passed) {
        throw "Offline startup failed with exit code $exitCode and $stderrBytes stderr bytes"
    }
    [pscustomobject]$evidence | Format-List
}
finally {
    if ($null -eq $previousOffline) { Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue }
    else { $env:HF_HUB_OFFLINE = $previousOffline }
    if ($null -eq $previousTelemetry) { Remove-Item Env:HF_HUB_DISABLE_TELEMETRY -ErrorAction SilentlyContinue }
    else { $env:HF_HUB_DISABLE_TELEMETRY = $previousTelemetry }
}
