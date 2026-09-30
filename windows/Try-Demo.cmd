@echo off
rem Try DownloadWatch with made-up data, without installing anything. Close this window to stop.
cd /d "%~dp0"
set DEMO=true
set HOST=127.0.0.1
set PORT=8090
set DATA_DIR=%~dp0demo-data
set DOWNLOADWATCH_CONFIG=
echo.
echo DownloadWatch demo: http://localhost:8090  (close this window to stop)
echo.
start "" http://localhost:8090
"%~dp0python\python.exe" "%~dp0app\downloadwatch.py"
pause
