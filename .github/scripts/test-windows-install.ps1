# CI: install the Windows zip unattended (demo mode), check it, upgrade in place, uninstall. Runs on windows-latest.
param([Parameter(Mandatory)][string]$Zip)
$ErrorActionPreference = "Stop"
function Check($ok, $what) { if (-not $ok) { throw "FAILED: $what" } ; Write-Host "ok  $what" }
function Get-Status($url, $cred) {
    try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri $url -Credential $cred).StatusCode }
    catch { if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } ; return 0 }
}

$dir = Join-Path $env:RUNNER_TEMP "dw"
Expand-Archive -Path $Zip -DestinationPath $dir -Force
$pkg = Get-ChildItem $dir -Directory | Select-Object -First 1
$pw = ConvertTo-SecureString "ci-password-123456" -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential("admin", $pw)

& "$($pkg.FullName)\python\python.exe" "$($pkg.FullName)\app\downloadwatch.py" --version
& "$($pkg.FullName)\Install-DownloadWatch.ps1" -Unattended -Demo -Password $pw -TimeZone "America/Denver"

Check ((Get-ScheduledTask -TaskName DownloadWatch).State -eq "Running") "scheduled task running"
Check ((Get-NetFirewallRule -DisplayName "DownloadWatch for Plex").Enabled -eq "True") "firewall rule"
Check ((Get-Status "http://127.0.0.1:8090/health" $null) -eq 200) "health 200"
Check ((Get-Status "http://127.0.0.1:8090/" $null) -eq 401) "dashboard needs login"
Check ((Get-Status "http://127.0.0.1:8090/" $cred) -eq 200) "dashboard with login"
$sum = Invoke-RestMethod -Uri "http://127.0.0.1:8090/api/summary" -Credential $cred
Check ($sum.all -gt 100 -and $sum.timezone -eq "America/Denver") "demo data and time zone (tzdata bundled)"
$cfg = "$env:ProgramData\DownloadWatch\downloadwatch.env"
$acl = (Get-Acl $cfg).Access | ForEach-Object { $_.IdentityReference.Value }
Check (-not ($acl -match "Users|Everyone")) "settings file not readable by Users"
Check (Test-Path "$env:ProgramData\DownloadWatch\logs\app.log") "log file written"

# upgrade in place keeps settings
& "$($pkg.FullName)\Install-DownloadWatch.ps1" -Unattended
Start-Sleep -Seconds 3
Check ((Get-Status "http://127.0.0.1:8090/" $cred) -eq 200) "still works after re-install (settings kept)"

& "C:\Program Files\DownloadWatch\Uninstall-DownloadWatch.ps1" -RemoveData
Check (-not (Get-ScheduledTask -TaskName DownloadWatch -ErrorAction SilentlyContinue)) "task removed"
Check (-not (Test-Path "C:\Program Files\DownloadWatch")) "program folder removed"
Check (-not (Test-Path "$env:ProgramData\DownloadWatch")) "data removed with -RemoveData"
Check ((Get-Status "http://127.0.0.1:8090/health" $null) -eq 0) "nothing listening"
Write-Host "Windows install test passed"
