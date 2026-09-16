@echo off
setlocal
cd /d "%~dp0"
title SNS Media Collector v0.1.9 Full Test
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_and_run.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" full_test.py
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo Full test failed with code %RC%.
if "%RC%"=="0" echo Full test passed.
pause
exit /b %RC%
