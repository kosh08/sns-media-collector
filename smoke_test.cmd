@echo off
setlocal
cd /d "%~dp0"
title SNS Media Collector Smoke Test
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_and_run.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" smoke_test.py
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo Smoke test failed with code %RC%.
if "%RC%"=="0" echo Smoke test passed.
pause
exit /b %RC%
