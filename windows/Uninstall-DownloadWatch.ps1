<#
.SYNOPSIS
  Removes DownloadWatch for Plex from Windows. Keeps your history and settings unless you add -RemoveData.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File "C:\Program Files\DownloadWatch\Uninstall-DownloadWatch.ps1"
  ... -RemoveData     also deletes C:\ProgramData\DownloadWatch (history, settings, Plex token)
#>
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [string]$InstallDir = (Join-Path $env:ProgramFiles "DownloadWatch"),
    [string]$DataRoot = (Join-Path $env:ProgramData "DownloadWatch")
)
$ErrorActionPreference = "Stop"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Please run this from an elevated PowerShell." }

if (Get-ScheduledTask -TaskName "DownloadWatch" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "DownloadWatch" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "DownloadWatch" -Confirm:$false
    Write-Host "Removed Scheduled Task 'DownloadWatch'."
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Get-NetFirewallRule -DisplayName "DownloadWatch for Plex" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Removed firewall rule."

# This script may itself be in $InstallDir: step out of the folder first; anything still locked is reported.
if (Test-Path $InstallDir) {
    Set-Location $env:TEMP
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) { Write-Host "Some files in $InstallDir were in use; delete the folder after a restart." -ForegroundColor Yellow }
    else { Write-Host "Removed $InstallDir." }
}
if ($RemoveData) {
    Remove-Item -Recurse -Force $DataRoot -ErrorAction SilentlyContinue
    Write-Host "Removed $DataRoot (history, settings and Plex token)."
} else {
    Write-Host "Kept your history and settings in $DataRoot (add -RemoveData to delete them)."
}
