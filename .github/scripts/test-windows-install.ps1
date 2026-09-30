# CI: install the Windows zip unattended (demo mode), check it, upgrade in place, uninstall. Runs on windows-latest.
# Failures are reported as ::error:: annotations (with the app log) so they are readable without signing in.
param([Parameter(Mandatory)][string]$Zip)
$ErrorActionPreference = "Stop"
$auth = @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:ci-password-123456")) }

function Check($ok, $what) { if (-not $ok) { throw "check failed: $what" } ; Write-Host "ok  $what" }
function Get-Status([string]$url, [switch]$Login) {
    $p = @{ UseBasicParsing = $true; TimeoutSec = 5; Uri = $url }
    if ($Login) { $p.Headers = $auth }
    try { return [int](Invoke-WebRequest @p).StatusCode }
    catch { if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } ; return 0 }
}
function Annotate([string]$text) {
    # one annotation per call; GitHub annotations take a single line, so encode newlines
    Write-Host ("::error::" + ($text -replace "%", "%25" -replace "`r", "" -replace "`n", "%0A"))
}

try {
    $dir = Join-Path $env:RUNNER_TEMP "dw"
    Expand-Archive -Path $Zip -DestinationPath $dir -Force
    $pkg = Get-ChildItem $dir -Directory | Select-Object -First 1
    $pw = ConvertTo-SecureString "ci-password-123456" -AsPlainText -Force

    & "$($pkg.FullName)\python\python.exe" "$($pkg.FullName)\app\downloadwatch.py" --version
    & "$($pkg.FullName)\Install-DownloadWatch.ps1" -Unattended -Demo -Password $pw -TimeZone "America/Denver"

    Check ((Get-ScheduledTask -TaskName DownloadWatch).State -eq "Running") "scheduled task running"
    Check ((Get-NetFirewallRule -DisplayName "DownloadWatch for Plex").Enabled -eq "True") "firewall rule"
    Check ((Get-Status "http://127.0.0.1:8090/health") -eq 200) "health 200"
    Check ((Get-Status "http://127.0.0.1:8090/") -eq 401) "dashboard needs login"
    Check ((Get-Status "http://127.0.0.1:8090/" -Login) -eq 200) "dashboard with login"
    $sum = Invoke-RestMethod -Uri "http://127.0.0.1:8090/api/summary" -Headers $auth
    Check ($sum.all -gt 100 -and $sum.timezone -eq "America/Denver") "demo data and time zone (tzdata bundled)"
    $cfg = "$env:ProgramData\DownloadWatch\downloadwatch.env"
    $acl = (Get-Acl $cfg).Access | ForEach-Object { $_.IdentityReference.Value }
    Check (-not ($acl -match "Users|Everyone")) "settings file not readable by Users ($($acl -join ', '))"
    Check (Test-Path "$env:ProgramData\DownloadWatch\logs\app.log") "log file written"

    # upgrade in place keeps settings
    & "$($pkg.FullName)\Install-DownloadWatch.ps1" -Unattended
    Check ((Get-Status "http://127.0.0.1:8090/" -Login) -eq 200) "still works after re-install (settings kept)"
    $acl = (Get-Acl $cfg).Access | ForEach-Object { $_.IdentityReference.Value }
    Check (($acl -match "Administrators") -and -not ($acl -match "Users|Everyone")) "settings ACL after re-install ($($acl -join ', '))"

    & "C:\Program Files\DownloadWatch\Uninstall-DownloadWatch.ps1" -RemoveData
    Check (-not (Get-ScheduledTask -TaskName DownloadWatch -ErrorAction SilentlyContinue)) "task removed"
    Check (-not (Test-Path "C:\Program Files\DownloadWatch")) "program folder removed"
    Check (-not (Test-Path "$env:ProgramData\DownloadWatch")) "data removed with -RemoveData"
    Check ((Get-Status "http://127.0.0.1:8090/health") -eq 0) "nothing listening"
    Write-Host "Windows install test passed"
} catch {
    Annotate ("Windows install test: " + $_.Exception.Message + "`n" + $_.InvocationInfo.PositionMessage)
    $t = Get-ScheduledTask -TaskName DownloadWatch -ErrorAction SilentlyContinue
    if ($t) {
        $info = $t | Get-ScheduledTaskInfo
        Annotate ("Task state: $($t.State); last result: $($info.LastTaskResult); last run: $($info.LastRunTime)")
    }
    $log = "$env:ProgramData\DownloadWatch\logs\app.log"
    if (Test-Path $log) { Annotate ("app.log:`n" + ((Get-Content $log -Tail 30) -join "`n")) }
    exit 1
}
