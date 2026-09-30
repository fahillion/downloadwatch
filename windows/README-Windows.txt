DownloadWatch for Plex - Windows
================================

A permanent history of who downloaded what from your Plex server.
Project page: https://github.com/fahillion/downloadwatch

Nothing else to install: this folder contains the official Python from python.org
(embeddable build, in "python\") plus the app. Windows 10/11 or Server 2016+, 64-bit.


TRY IT FIRST (made-up data, no Plex needed)
  Double-click Try-Demo.cmd. Your browser opens http://localhost:8090.
  Close the black window to stop it.


INSTALL (runs in the background, starts with Windows)
  1. Right-click Start > "Terminal (Admin)" or "Windows PowerShell (Admin)".
  2. Go to this folder, for example:
       cd "$env:USERPROFILE\Downloads\DownloadWatch-windows-x64"
  3. Run:
       powershell -ExecutionPolicy Bypass -File .\Install-DownloadWatch.ps1
  4. Answer the questions: your Plex server's LAN address (e.g. http://192.168.1.10:32400),
     your time zone, and a dashboard password.
  5. Open the address it prints (http://<this PC>:8090), log in as "admin",
     click "Get a code" and enter it at https://plex.tv/link with the account that OWNS your Plex server.

  Where things go:
    Program:   C:\Program Files\DownloadWatch
    Settings:  C:\ProgramData\DownloadWatch\downloadwatch.env   (Administrators and SYSTEM only)
    History:   C:\ProgramData\DownloadWatch\data\               (back this folder up)
    Log:       C:\ProgramData\DownloadWatch\logs\app.log
    Runs as:   Scheduled Task "DownloadWatch" (at startup, as SYSTEM, restarts if it stops)
    Firewall:  TCP 8090 allowed on Private and Domain networks only

  Change settings: edit downloadwatch.env as Administrator, then in an admin PowerShell:
       Restart-ScheduledTask DownloadWatch        (or just re-run the installer)

  Upgrade: download the new zip, unzip, run Install-DownloadWatch.ps1 again and keep your settings.

  Uninstall (keeps history unless you add -RemoveData):
       powershell -ExecutionPolicy Bypass -File "C:\Program Files\DownloadWatch\Uninstall-DownloadWatch.ps1"


NOTES
  * Windows may warn that the scripts came from the internet. Right-click the zip > Properties >
    tick "Unblock" before unzipping, or run the commands above (they bypass the prompt for this run only).
  * If your network is set to "Public" in Windows, other PCs can't reach port 8090. Set it to Private
    (Settings > Network & internet > your connection > Private network).
  * DownloadWatch can run on any always-on PC on the same network as Plex, including the Plex PC itself.
  * Not affiliated with Plex Inc.
