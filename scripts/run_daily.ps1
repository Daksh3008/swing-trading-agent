# STA daily run - sync, scan, track, back up.
# Registered once with Windows Task Scheduler (see README). Never auto-registered.
#
#   schtasks /Create /SC DAILY /ST 18:30 /TN "STA Daily" /TR ^
#     "powershell -ExecutionPolicy Bypass -File <repo>\scripts\run_daily.ps1"

$ErrorActionPreference = "Continue"
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
Write-Step "daily run started"

Invoke-Step "sync" @("-m", "sta", "sync", "--catch-up")
Invoke-Step "scan" @("-m", "sta", "scan", "--commit")
Invoke-Step "track" @("-m", "sta", "track", "--catch-up")
Invoke-Step "backup" @("-m", "sta", "backup")

if ($failed) {
    Write-Step "daily run finished with failures"
    exit 1
}
Write-Step "daily run finished"
exit 0
