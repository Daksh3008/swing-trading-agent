# STA logon catch-up - fills the gap after the laptop has been off.
# Registered once with Windows Task Scheduler (see README), or left to the app's
# own startup catch-up, which uses the same idempotent sequence.
#
#   schtasks /Create /SC ONLOGON /TN "STA Catch-up" /TR ^
#     "powershell -ExecutionPolicy Bypass -File <repo>\scripts\run_catchup.ps1"
#
# Every command is idempotent: sync skips when a real run already covered the
# session, the scan inserts nothing twice, and the tracker only replays sessions
# it has not processed.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp = Get-Date -Format "yyyyMMdd"
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "catchup_$stamp.log"
$env:PYTHONIOENCODING = "utf-8"

function Write-Step([string]$text) {
    "[$(Get-Date -Format 'HH:mm:ss')] $text" | Out-File -FilePath $log -Append -Encoding utf8
}

function Invoke-Step([string]$name, [string[]]$arguments) {
    Write-Step $name
    & python @arguments 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Step "$name failed (exit $code)"
        $script:failed = $true
    }
}

$failed = $false
Write-Step "catch-up started"

Invoke-Step "sync" @("-m", "sta", "sync", "--catch-up")
Invoke-Step "scan" @("-m", "sta", "scan", "--commit")
Invoke-Step "track" @("-m", "sta", "track", "--catch-up")

if ($failed) {
    Write-Step "catch-up finished with failures"
    exit 1
}
Write-Step "catch-up finished"
exit 0
