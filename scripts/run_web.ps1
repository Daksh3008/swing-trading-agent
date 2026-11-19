# Supervisor for the local STA web app.
#
# The "STA Web" scheduled task runs this at logon, but it is safe to run by
# hand. It refuses to start a second copy on the same port and restarts the app
# with a bounded backoff if it dies unexpectedly.
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1 -NoRestart
#   powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1 -Port 8010

param(
    [int]$Port = 8000,
    [switch]$NoRestart
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("web_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Log([string]$Message) {
    Add-Content -Path $log -Value ("{0} {1}" -f (Get-Date -Format "s"), $Message)
}

function Test-Port([int]$Number) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Number -State Listen -ErrorAction Stop)
    } catch {
        return $false
    }
}

$env:STA_PORT = "$Port"
$attempt = 0

while ($true) {
    if (Test-Port -Number $Port) {
        Write-Log "port $Port is already served; supervisor exiting"
        break
    }
    if ($attempt -gt 0) {
        $delay = [Math]::Min(60, 5 * $attempt)
        Write-Log "restarting in $delay s (attempt $attempt)"
        Start-Sleep -Seconds $delay
    }
    Write-Log "starting python -m sta.web on port $Port"
    & python -m sta.web 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    $code = $LASTEXITCODE
    Write-Log "app exited with code $code"
    if ($NoRestart) { break }
    $attempt++
}
