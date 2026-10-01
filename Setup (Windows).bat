@echo off
title DJI Sorter setup
cd /d "%~dp0"
set "APPDIR=%LOCALAPPDATA%\DJISorter\app"
set "EXE=%APPDIR%\DJI Sorter\DJI Sorter.exe"

where py >nul 2>nul
if errorlevel 1 (
  echo Python is needed once to build the app. Installing it with winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python installed. Close this window and double-click "Setup (Windows).bat" again.
  pause
  exit /b
)

rem Stop any running copy (e.g. the SD card watcher) so its files can be replaced
taskkill /f /im "DJI Sorter.exe" >nul 2>nul
if exist "%~dp0DJI Sorter.exe" del /f /q "%~dp0DJI Sorter.exe" >nul 2>nul

echo [1/4] Installing build tools...
py -m pip install --user --upgrade pyinstaller -r requirements.txt
if errorlevel 1 goto fail

echo.
echo [2/4] Building DJI Sorter (takes a minute)...
rem Folder build (not one-file): runs straight from disk, no unpacking to Temp,
rem which antivirus / OneDrive can block. Installed outside OneDrive on purpose.
py -m PyInstaller --noconfirm --clean --onedir --windowed --name "DJI Sorter" ^
  --add-data "%~dp0dji_sorter.py;app_code" --add-data "%~dp0sorter_core.py;app_code" --add-data "%~dp0version.json;app_code" ^
  --collect-all tkinterdnd2 --distpath "%APPDIR%" --workpath "%TEMP%\djisorter-build" --specpath "%TEMP%\djisorter-build" ^
  launcher.py
if not exist "%EXE%" goto fail

echo.
echo [3/4] Optional extras
winget list -e --id Gyan.FFmpeg >nul 2>nul
if errorlevel 1 (
  choice /m "Install ffmpeg (needed for LUTs) and exiftool (better GPS from videos)"
  if not errorlevel 2 (
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    winget install -e --id OliverBetz.ExifTool --accept-package-agreements --accept-source-agreements
  )
) else (
  echo ffmpeg is already installed, skipping.
)

echo.
echo [4/4] Adding a desktop shortcut and turning on SD card auto-open...
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\DJI Sorter.lnk');$s.TargetPath='%EXE%';$s.WorkingDirectory='%APPDIR%\DJI Sorter';$s.Save()"
"%EXE%" --install-autostart

echo.
echo Done! Open "DJI Sorter" from your desktop.
echo (The app itself lives in %APPDIR%)
pause
exit /b

:fail
echo.
echo Something went wrong above. Copy the error text and send it to Claude.
pause
