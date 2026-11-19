# STA logon catch-up — fills the gap after the laptop has been off.
# Registered once with Windows Task Scheduler (see README).
#
#   schtasks /Create /SC ONLOGON /TN "STA Catch-up" /TR ^
#     "powershell -ExecutionPolicy Bypass -File <repo>\scripts\run_catchup.ps1"
#
# Both commands are idempotent: sync skips when the daily run already covered
# today, and the tracker only replays sessions it has not processed yet.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp = Get-Date -Format "yyyyMMdd"
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "catchup_$stamp.log"
$env:PYTHONIOENCODING = "utf-8"

"[$(Get-Date -Format 'HH:mm:ss')] catch-up started" | Out-File -FilePath $log -Append -Encoding utf8

& python -m sta sync --catch-up *>> $log
& python -m sta scan --commit *>> $log
& python -m sta track --catch-up *>> $log

"[$(Get-Date -Format 'HH:mm:ss')] catch-up finished" | Out-File -FilePath $log -Append -Encoding utf8
exit 0
