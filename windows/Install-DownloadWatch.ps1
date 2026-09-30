<#
.SYNOPSIS
  Installs or upgrades DownloadWatch for Plex on Windows (no Docker, no separate Python needed).

.DESCRIPTION
  Run from the unzipped DownloadWatch folder in an elevated PowerShell:
      powershell -ExecutionPolicy Bypass -File .\Install-DownloadWatch.ps1

  What it does:
    * copies the program to  C:\Program Files\DownloadWatch   (bundled official Python + the app)
    * keeps settings/data in C:\ProgramData\DownloadWatch     (only SYSTEM and Administrators can read it:
                                                                it holds your dashboard password and Plex token)
    * registers a Scheduled Task "DownloadWatch" that starts at boot as SYSTEM and restarts if it stops
    * allows TCP <Port> through Windows Firewall on Private and Domain networks only
  Re-running it upgrades the program and keeps your settings and history.

.EXAMPLE
  .\Install-DownloadWatch.ps1
  .\Install-DownloadWatch.ps1 -PlexUrl http://192.168.1.10:32400 -TimeZone Europe/London -Unattended -Password (Read-Host -AsSecureString)
#>
[CmdletBinding()]
param(
    [string]$PlexUrl,
    [string]$Username = "admin",
    [securestring]$Password,
    [string]$TimeZone,
    [int]$Port = 8090,
    [switch]$Demo,
    [switch]$Unattended,
    [switch]$NoFirewall,
    [string]$InstallDir = (Join-Path $env:ProgramFiles "DownloadWatch"),
    [string]$DataRoot = (Join-Path $env:ProgramData "DownloadWatch")
)
$ErrorActionPreference = "Stop"
$TaskName = "DownloadWatch"
$FirewallName = "DownloadWatch for Plex"
$Source = $PSScriptRoot

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

# ---------------------------------------------------------------- checks
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Please run this from an elevated PowerShell (right-click PowerShell > Run as administrator)." }
foreach ($need in @("python\python.exe", "app\downloadwatch.py")) {
    if (-not (Test-Path (Join-Path $Source $need))) { throw "Missing $need. Run this script from the unzipped DownloadWatch folder." }
}
$version = (& (Join-Path $Source "python\python.exe") (Join-Path $Source "app\downloadwatch.py") --version)
Say "`n$version - Windows installer`n" Cyan

$configFile = Join-Path $DataRoot "downloadwatch.env"
$dataDir = Join-Path $DataRoot "data"
$logFile = Join-Path $DataRoot "logs\app.log"

# Common Windows time zones -> tz database names (for the suggested default)
$tzMap = @{
    "Eastern Standard Time" = "America/New_York"; "Central Standard Time" = "America/Chicago";
    "Mountain Standard Time" = "America/Denver"; "US Mountain Standard Time" = "America/Phoenix";
    "Pacific Standard Time" = "America/Los_Angeles"; "Alaskan Standard Time" = "America/Anchorage";
    "Hawaiian Standard Time" = "Pacific/Honolulu"; "Atlantic Standard Time" = "America/Halifax";
    "Newfoundland Standard Time" = "America/St_Johns"; "Canada Central Standard Time" = "America/Regina";
    "GMT Standard Time" = "Europe/London"; "W. Europe Standard Time" = "Europe/Berlin";
    "Romance Standard Time" = "Europe/Paris"; "Central Europe Standard Time" = "Europe/Budapest";
    "Central European Standard Time" = "Europe/Warsaw"; "E. Europe Standard Time" = "Europe/Chisinau";
    "FLE Standard Time" = "Europe/Kiev"; "GTB Standard Time" = "Europe/Bucharest"; "Russian Standard Time" = "Europe/Moscow";
    "AUS Eastern Standard Time" = "Australia/Sydney"; "E. Australia Standard Time" = "Australia/Brisbane";
    "Cen. Australia Standard Time" = "Australia/Adelaide"; "W. Australia Standard Time" = "Australia/Perth";
    "New Zealand Standard Time" = "Pacific/Auckland"; "Tokyo Standard Time" = "Asia/Tokyo";
    "China Standard Time" = "Asia/Shanghai"; "India Standard Time" = "Asia/Kolkata"; "Singapore Standard Time" = "Asia/Singapore";
    "E. South America Standard Time" = "America/Sao_Paulo"; "SA Pacific Standard Time" = "America/Bogota";
    "Central Standard Time (Mexico)" = "America/Mexico_City"; "South Africa Standard Time" = "Africa/Johannesburg";
    "UTC" = "UTC"
}

function Read-Plain([string]$prompt, [string]$default) {
    $suffix = ""
    if ($default) { $suffix = " [$default]" }
    $answer = Read-Host "$prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim()
}

function To-Plain([securestring]$s) {
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

# ---------------------------------------------------------------- settings
$keep = $false
if (Test-Path $configFile) {
    if ($Unattended -and -not $PlexUrl) { $keep = $true }
    elseif (-not $Unattended) {
        $keep = (Read-Plain "Existing settings found in $configFile. Keep them? (Y/n)" "Y") -notmatch '^[nN]'
    }
}

if (-not $keep) {
    if (-not $Demo) {
        while (-not $PlexUrl) {
            if ($Unattended) { throw "-PlexUrl is required (e.g. http://192.168.1.10:32400)." }
            $PlexUrl = Read-Plain "Plex server address (LAN), e.g. http://192.168.1.10:32400" ""
        }
        $PlexUrl = $PlexUrl.TrimEnd("/")
        if ($PlexUrl -notmatch '^https?://') { $PlexUrl = "http://$PlexUrl" }
        if ($PlexUrl -notmatch ':\d+$' -and $PlexUrl -notmatch ':\d+/') { $PlexUrl = "${PlexUrl}:32400" }
        try {
            $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 -Uri "$PlexUrl/identity"
            Say "  Plex answered at $PlexUrl" Green
        } catch {
            Say "  Warning: no answer from $PlexUrl/identity ($($_.Exception.Message)). Continuing; fix it later in $configFile." Yellow
        }
    }
    if (-not $TimeZone) {
        $guess = $tzMap[[TimeZoneInfo]::Local.Id]
        if (-not $guess) { $guess = "UTC" }
        if ($Unattended) { $TimeZone = $guess } else { $TimeZone = Read-Plain "Time zone (tz database name)" $guess }
    }
    # (prints ok/bad instead of failing: native-command errors would stop the script in Windows PowerShell 5.1)
    $check = & (Join-Path $Source "python\python.exe") -c "import zoneinfo,sys;print('ok' if sys.argv[1] in zoneinfo.available_timezones() else 'bad')" $TimeZone
    if ("$check".Trim() -ne "ok") { throw "Unknown time zone '$TimeZone'. Use a name like America/Chicago or Europe/London." }
    if (-not $Password) {
        if ($Unattended) { throw "-Password is required with -Unattended." }
        do {
            $p1 = To-Plain (Read-Host "Choose a dashboard password (12+ characters)" -AsSecureString)
            $p2 = To-Plain (Read-Host "Type it again" -AsSecureString)
            if ($p1 -ne $p2) { Say "  They don't match. Try again." Yellow; $p1 = "" }
            elseif ($p1.Length -lt 12) { Say "  Too short. Use at least 12 characters." Yellow; $p1 = "" }
        } while (-not $p1)
        $plainPassword = $p1
    } else {
        $plainPassword = To-Plain $Password
    }
    if ($plainPassword -match '[\r\n]') { throw "The password can't contain line breaks." }
}

# ---------------------------------------------------------------- stop an existing install
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Say "Stopping the running DownloadWatch..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------- program files
Say "Installing program files to $InstallDir ..."
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($dir in @("python", "lib", "app")) {
    $target = Join-Path $InstallDir $dir
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    Copy-Item -Recurse -Force (Join-Path $Source $dir) $target
}
foreach ($file in @("Uninstall-DownloadWatch.ps1", "Try-Demo.cmd", "README-Windows.txt", "LICENSE.txt")) {
    if (Test-Path (Join-Path $Source $file)) { Copy-Item -Force (Join-Path $Source $file) $InstallDir }
}

# ---------------------------------------------------------------- data folder (SYSTEM + Administrators only)
New-Item -ItemType Directory -Force -Path $dataDir, (Split-Path $logFile) | Out-Null
# Protect the folder itself, then make everything inside inherit from it (per-file grants with /T would strip
# the files' inherited entries and could lock Administrators out of an existing settings file).
& icacls $DataRoot /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not set permissions on $DataRoot." }
if (Get-ChildItem -Force $DataRoot) {
    & icacls (Join-Path $DataRoot "*") /reset /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not reset permissions inside $DataRoot." }
}

if (-not $keep) {
    $lines = @(
        "# DownloadWatch for Plex settings (written by Install-DownloadWatch.ps1 on $(Get-Date -Format s))."
        "# Edit, then restart:  Restart-ScheduledTask DownloadWatch   (or re-run the installer)."
        "PLEX_URL=$PlexUrl"
        "AUTH_USERNAME=$Username"
        "AUTH_PASSWORD=$plainPassword"
        "TIMEZONE=$TimeZone"
        "PORT=$Port"
        "HOST=0.0.0.0"
        "DATA_DIR=$dataDir"
        "LOG_FILE=$logFile"
    )
    if ($Demo) { $lines += "DEMO=true" }
    [IO.File]::WriteAllText($configFile, ($lines -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding($false)))
    $plainPassword = $null
    Say "Settings saved to $configFile" Green
} else {
    Say "Keeping existing settings in $configFile" Green
    $portLine = Select-String -Path $configFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($portLine) { $Port = [int]$portLine.Matches[0].Groups[1].Value }
}

# ---------------------------------------------------------------- firewall
if (-not $NoFirewall) {
    Get-NetFirewallRule -DisplayName $FirewallName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $FirewallName -Direction Inbound -Protocol TCP -LocalPort $Port `
        -Profile Private, Domain -Action Allow -Description "Dashboard for DownloadWatch for Plex" | Out-Null
    Say "Firewall: TCP $Port allowed on Private and Domain networks." Green
}

# ---------------------------------------------------------------- scheduled task
$python = Join-Path $InstallDir "python\python.exe"
$script = Join-Path $InstallDir "app\downloadwatch.py"
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --config `"$configFile`"" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "DownloadWatch for Plex: records Plex downloads. Settings: $configFile" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Say "Scheduled Task '$TaskName' registered (starts at boot) and started." Green

# ---------------------------------------------------------------- check
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$Port/health"
        $healthy = $true; break
    } catch {
        if ($_.Exception.Response) { $healthy = $true; break }   # 503 = running, not yet signed in to Plex
    }
}
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
       Select-Object -First 1).IPAddress
if (-not $ip) { $ip = $env:COMPUTERNAME }
if ($healthy) {
    Say "`nDownloadWatch is running." Green
    Say "Open  http://${ip}:$Port  (or http://localhost:$Port on this PC), log in as '$Username'," Cyan
    Say "then click 'Get a code' and enter it at plex.tv/link with the account that owns your Plex server." Cyan
} else {
    Say "`nDownloadWatch did not answer on port $Port yet. Check $logFile" Yellow
}
Say "Uninstall: $InstallDir\Uninstall-DownloadWatch.ps1`n"
