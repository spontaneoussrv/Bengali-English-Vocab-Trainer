@echo off
title Build VocabTrainer.exe
cd /d "%~dp0"
set PY=
where py >/dev/null 2>&1 && set PY=py -3
if not defined PY where python >/dev/null 2>&1 && set PY=python
if not defined PY (
  echo   Python was not found. Install it from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)
echo   Installing the build tools, this takes a minute the first time...
%PY% -m pip install --disable-pip-version-check --upgrade pyinstaller pystray pillow
echo.
echo   Building VocabTrainer.exe with the icon and no console window...
%PY% -m PyInstaller --noconsole --onefile --clean --name VocabTrainer ^
  --icon VocabTrainer.ico --add-data "VocabTrainer.ico;." ^
  --collect-all pystray --collect-all PIL BengaliEnglishTrainer.py
if exist "dist\\VocabTrainer.exe" (
  copy /y "dist\\VocabTrainer.exe" "%~dp0VocabTrainer.exe" >nul
  echo.
  echo   Done. VocabTrainer.exe is in this folder with its icon.
  echo   The build and dist folders can be deleted.
) else (
  echo.
  echo   The build did not produce an exe. The messages above say why.
)
echo.
pause
