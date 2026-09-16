@echo off
setlocal
cd /d "%~dp0"
title SNS Media Collector Diagnostics
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_and_run.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" diagnose.py
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
