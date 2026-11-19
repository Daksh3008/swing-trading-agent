# STA daily run — sync, scan, track, back up.
# Registered once with Windows Task Scheduler (see README). Never auto-registered.
#
#   schtasks /Create /SC DAILY /ST 18:30 /TN "STA Daily" /TR ^
#     "powershell -ExecutionPolicy Bypass -File <repo>\scripts\run_daily.ps1"

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp = Get-Date -Format "yyyyMMdd"
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "daily_$stamp.log"
$env:PYTHONIOENCODING = "utf-8"

function Write-Step([string]$text) {
    "[$(Get-Date -Format 'HH:mm:ss')] $text" | Out-File -FilePath $log -Append -Encoding utf8
}

Write-Step "daily run started"

Write-Step "sync"
& python -m sta sync --catch-up *>> $log
$syncExit = $LASTEXITCODE

Write-Step "scan"
& python -m sta scan --commit *>> $log

Write-Step "track"
& python -m sta track --catch-up *>> $log

Write-Step "backup"
& python -m sta backup *>> $log

Write-Step "daily run finished (sync exit $syncExit)"
exit $syncExit
