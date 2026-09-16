@echo off
setlocal
cd /d "%~dp0"
title SNS Media Collector v0.1.9 Debug Run
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_and_run.cmd first.
  pause
  exit /b 1
)
del /q crash.log 2>nul
".venv\Scripts\python.exe" launcher.py
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo Application failed. Error code: %RC%
  if exist crash.log type crash.log
  echo.
  pause
)
exit /b %RC%
