@echo off
setlocal
cd /d "%~dp0"
title SNS Media Collector Build
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_and_run.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name SNSMediaCollector launcher.py
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console --name gallery-dl gallery_dl_launcher.py
if errorlevel 1 goto fail
mkdir "dist\SNSMediaCollector\bin" 2>nul
move /Y "dist\gallery-dl.exe" "dist\SNSMediaCollector\bin\gallery-dl.exe" >nul
if errorlevel 1 goto fail
echo.
echo Build complete: dist\SNSMediaCollector\SNSMediaCollector.exe
pause
exit /b 0
:fail
echo.
echo Build failed.
pause
exit /b 1
