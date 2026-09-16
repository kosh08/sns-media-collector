@echo off
setlocal
cd /d "%~dp0"
title SNS Media Collector v0.1.9 Setup

where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python

echo.
echo [ERROR] Python 3 was not found.
echo Install Python 3.10 or newer, then run this file again.
echo.
pause
exit /b 1

:use_py
py -3 bootstrap.py
set RC=%ERRORLEVEL%
goto done

:use_python
python bootstrap.py
set RC=%ERRORLEVEL%
goto done

:done
if not "%RC%"=="0" (
  echo.
  echo Setup or launch failed. Error code: %RC%
  echo Check setup.log and crash.log in this folder.
  echo.
  pause
)
exit /b %RC%
