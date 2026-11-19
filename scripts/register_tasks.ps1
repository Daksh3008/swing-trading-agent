# Register (or remove) the two Windows scheduled tasks for STA.
# Run once from an elevated PowerShell if the daily task must run while the
# user is logged off. Nothing else in the project registers tasks itself.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Remove

param(
    [string]$Time = "18:30",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$daily = "powershell -ExecutionPolicy Bypass -File `"$repo\scripts\run_daily.ps1`""
$catchup = "powershell -ExecutionPolicy Bypass -File `"$repo\scripts\run_catchup.ps1`""

if ($Remove) {
    schtasks /Delete /TN "STA Daily" /F 2>$null
    schtasks /Delete /TN "STA Catch-up" /F 2>$null
    Write-Host "Removed STA Daily and STA Catch-up."
    exit 0
}

schtasks /Create /F /SC DAILY /ST $Time /TN "STA Daily" /TR $daily | Out-Null
schtasks /Create /F /SC ONLOGON /TN "STA Catch-up" /TR $catchup | Out-Null

Write-Host "Registered:"
schtasks /Query /TN "STA Daily" /FO LIST | Select-String "TaskName|Next Run|Status"
schtasks /Query /TN "STA Catch-up" /FO LIST | Select-String "TaskName|Next Run|Status"
Write-Host ""
Write-Host "Start the app with: python -m sta.web"
