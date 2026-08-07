@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=%SCRIPT_DIR%..\runtime\data-engineering\.venv\Scripts\python.exe"
set "RQ_SITE_PACKAGES=%SCRIPT_DIR%..\runtime\ricequant-rqsdk\.venv\Lib\site-packages"
if not exist "%PYTHON_EXE%" (
  echo {"status":"FAIL_CLOSED","error":"central data-engineering Python runtime not found"}
  exit /b 2
)
if not exist "%RQ_SITE_PACKAGES%\rqdatac" (
  echo {"status":"FAIL_CLOSED","error":"Ricequant runtime package directory not found"}
  exit /b 2
)
if defined PYTHONPATH (
  set "PYTHONPATH=%RQ_SITE_PACKAGES%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%RQ_SITE_PACKAGES%"
)
"%PYTHON_EXE%" -X utf8 "%SCRIPT_DIR%run_rqdata_financial_pit_daily_update.py" %*
exit /b %ERRORLEVEL%
