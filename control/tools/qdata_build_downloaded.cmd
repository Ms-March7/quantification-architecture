@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0qdata_build_downloaded.ps1" %*
exit /b %ERRORLEVEL%
