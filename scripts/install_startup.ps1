# Install (or remove) a per-user logon launcher for the STA web app.
#
# The launcher is a tiny VBScript in the current user's Startup folder, so it
# runs at logon without the elevation that an ONLOGON scheduled task needs.
# The supervisor itself refuses to start a second copy on the same port.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove

param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$startup = [Environment]::GetFolderPath("Startup")
$launcher = Join-Path $startup "STA Web.vbs"

if ($Remove) {
    if (Test-Path -LiteralPath $launcher) {
        Remove-Item -LiteralPath $launcher -Force
        Write-Host "Removed $launcher"
    } else {
        Write-Host "Nothing to remove at $launcher"
    }
    exit 0
}

$repo = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $repo "scripts\run_web.ps1"
$line = 'CreateObject("WScript.Shell").Run "powershell -NoProfile -WindowStyle Hidden ' +
        '-ExecutionPolicy Bypass -File ""' + $supervisor + '""", 0, False'
Set-Content -LiteralPath $launcher -Value $line -Encoding Unicode
Write-Host "Installed $launcher"
Write-Host "It starts scripts\run_web.ps1 at logon and restarts the app if it dies."
