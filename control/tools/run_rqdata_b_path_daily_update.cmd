@echo off
setlocal
for %%I in ("%~dp0..\..") do set "QUANTIFICATION_ROOT=%%~fI"
cd /d "%QUANTIFICATION_ROOT%"
set "RQ_SITE_PACKAGES=%QUANTIFICATION_ROOT%\control\runtime\ricequant-rqsdk\.venv\Lib\site-packages"
if not exist "%RQ_SITE_PACKAGES%\rqdatac" (
  echo {"status":"FAIL_CLOSED","error":"Ricequant runtime package directory not found"}
  exit /b 2
)
if defined PYTHONPATH (
  set "PYTHONPATH=%RQ_SITE_PACKAGES%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%RQ_SITE_PACKAGES%"
)
"%QUANTIFICATION_ROOT%\control\runtime\data-engineering\.venv\Scripts\python.exe" -X utf8 "%QUANTIFICATION_ROOT%\control\tools\run_rqdata_b_path_daily_update.py" %*
exit /b %ERRORLEVEL%
