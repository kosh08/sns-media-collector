@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "SMC_BUILD_OUTPUT=portable_build\v0.3.0-%RANDOM%-%RANDOM%"
title SNS Media Collector - Portable Builder

echo ============================================================
echo SNS Media Collector - Portable Build
echo ============================================================
echo.

where py >nul 2>nul
if errorlevel 1 goto nopython

if not exist ".venv\Scripts\python.exe" (
  echo [1/7] Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 goto fail
) else (
  echo [1/7] Existing virtual environment found.
)

echo [2/7] Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto fail

echo [3/7] Running project tests...
".venv\Scripts\python.exe" full_test.py
if errorlevel 1 goto fail

echo [4/7] Running startup smoke test...
set QT_QPA_PLATFORM=offscreen
".venv\Scripts\python.exe" smoke_test.py
if errorlevel 1 goto fail
set QT_QPA_PLATFORM=

echo [5/7] Building SNSMediaCollector.exe...
mkdir %SMC_BUILD_OUTPUT% 2>nul
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onedir --windowed ^
  --name SNSMediaCollector ^
  --distpath %SMC_BUILD_OUTPUT% ^
  --workpath build\app ^
  launcher.py
if errorlevel 1 goto fail

echo [6/7] Building bundled gallery-dl.exe...
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onefile --console ^
  --name gallery-dl ^
  --distpath %SMC_BUILD_OUTPUT%\engine ^
  --workpath build\gallerydl ^
  --collect-all gallery_dl ^
  gallery_dl_launcher.py
if errorlevel 1 goto fail

mkdir "%SMC_BUILD_OUTPUT%\SNSMediaCollector\bin" 2>nul
copy /Y "%SMC_BUILD_OUTPUT%\engine\gallery-dl.exe" "%SMC_BUILD_OUTPUT%\SNSMediaCollector\bin\gallery-dl.exe" >nul
if errorlevel 1 goto fail

echo [7/7] Verifying portable output...
if not exist "%SMC_BUILD_OUTPUT%\SNSMediaCollector\SNSMediaCollector.exe" goto fail
if not exist "%SMC_BUILD_OUTPUT%\SNSMediaCollector\bin\gallery-dl.exe" goto fail
"%SMC_BUILD_OUTPUT%\SNSMediaCollector\bin\gallery-dl.exe" --version > portable_gallerydl_version.txt 2>&1
if errorlevel 1 goto fail

start "" /wait "%SMC_BUILD_OUTPUT%\SNSMediaCollector\SNSMediaCollector.exe" --self-test "%CD%\%SMC_BUILD_OUTPUT%\portable_self_test.json"
if errorlevel 1 goto fail
if not exist "%SMC_BUILD_OUTPUT%\portable_self_test.json" goto fail

>"%SMC_BUILD_OUTPUT%\SNSMediaCollector\PORTABLE_README.txt" echo SNS Media Collector Portable
>>"%SMC_BUILD_OUTPUT%\SNSMediaCollector\PORTABLE_README.txt" echo.
>>"%SMC_BUILD_OUTPUT%\SNSMediaCollector\PORTABLE_README.txt" echo Run SNSMediaCollector.exe directly.
>>"%SMC_BUILD_OUTPUT%\SNSMediaCollector\PORTABLE_README.txt" echo User settings and databases are stored separately in %%USERPROFILE%%\SNSMediaCollector by default.
>>"%SMC_BUILD_OUTPUT%\SNSMediaCollector\PORTABLE_README.txt" echo Replacing this app folder will not erase that data.

echo.
echo ============================================================
echo BUILD COMPLETE
echo.
echo %SMC_BUILD_OUTPUT%\SNSMediaCollector\SNSMediaCollector.exe
echo ============================================================
echo.
echo From now on, launch that EXE directly.
echo The existing %%USERPROFILE%%\SNSMediaCollector data is reused.
explorer "%SMC_BUILD_OUTPUT%\SNSMediaCollector"
pause
exit /b 0

:nopython
echo [ERROR] Python launcher 'py' was not found.
echo Install Python 3.10 or newer, then run this again.
pause
exit /b 10

:fail
echo.
echo [ERROR] Portable build failed.
echo Keep this window open and send the visible error to ChatGPT.
pause
exit /b 1
