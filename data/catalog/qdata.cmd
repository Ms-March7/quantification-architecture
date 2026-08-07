@echo off
setlocal EnableDelayedExpansion

if defined QUANT_DATA_PYTHON if exist "%QUANT_DATA_PYTHON%" (
  "%QUANT_DATA_PYTHON%" -X utf8 "%~dp0dataset_cli.py" %*
  exit /b !ERRORLEVEL!
)

set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" -X utf8 "%~dp0dataset_cli.py" %*
  exit /b !ERRORLEVEL!
)

where python >nul 2>nul
if !ERRORLEVEL! EQU 0 (
  python -X utf8 "%~dp0dataset_cli.py" %*
  exit /b !ERRORLEVEL!
)

where py >nul 2>nul
if !ERRORLEVEL! EQU 0 (
  py -3 -X utf8 "%~dp0dataset_cli.py" %*
  exit /b !ERRORLEVEL!
)

echo DATA ACCESS ERROR: Python 3 not found. Set QUANT_DATA_PYTHON. 1>&2
exit /b 9009
