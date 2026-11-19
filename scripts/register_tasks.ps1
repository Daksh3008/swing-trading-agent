# Register (or remove) the STA scheduled tasks and the logon web launcher.
#
# STA Daily uses schtasks and works unelevated.
# An ONLOGON scheduled task needs elevation on current Windows builds, so STA
# Catch-up is attempted and reported, while the web app is started by a
# per-user Startup-folder launcher that needs no elevation.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -NoWeb

param(
    [string]$Time = "18:30",
    [switch]$Remove,
    [switch]$NoWeb
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$daily = "powershell -ExecutionPolicy Bypass -File `"$repo\scripts\run_daily.ps1`""
$catchup = "powershell -ExecutionPolicy Bypass -File `"$repo\scripts\run_catchup.ps1`""

if ($Remove) {
    schtasks /Delete /TN "STA Daily" /F 2>$null | Out-Null
    schtasks /Delete /TN "STA Catch-up" /F 2>$null | Out-Null
    schtasks /Delete /TN "STA Web" /F 2>$null | Out-Null
    powershell -ExecutionPolicy Bypass -File "$repo\scripts\install_startup.ps1" -Remove
    Write-Host "Removed STA Daily, STA Catch-up, STA Web and the logon launcher."
    exit 0
}

schtasks /Create /F /SC DAILY /ST $Time /TN "STA Daily" /TR $daily | Out-Null

if ($LASTEXITCODE -eq 0) {
    Write-Host "Registered STA Daily at $Time."
} else {
    Write-Warning "Could not register STA Daily (exit $LASTEXITCODE). Try an elevated PowerShell."
}

# The catch-up task is the nicer logon hook but needs elevation for an ONLOGON
# trigger. Report it honestly instead of pretending it worked.
schtasks /Create /F /SC ONLOGON /TN "STA Catch-up" /TR $catchup 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Registered STA Catch-up at logon."
} else {
    Write-Warning ("STA Catch-up needs an elevated PowerShell (ONLOGON triggers are " +
                   "admin-only here). The web app still catches up on its own at startup.")
}

if (-not $NoWeb) {
    powershell -ExecutionPolicy Bypass -File "$repo\scripts\install_startup.ps1"
}

Write-Host ""
Write-Host "The app is kept alive at http://127.0.0.1:8000 by the logon launcher."
Write-Host "Stop it with: Get-NetTCPConnection -LocalPort 8000 | Stop-Process"
