@echo off
setlocal
title Vocab Trainer
pushd "%~dp0"
if errorlevel 1 (
  echo   Could not open the folder this file is in.
  echo   If you opened it from inside a zip, extract the zip first.
  pause
  exit /b 1
)
echo.
echo   Bengali to English Vocabulary Trainer
echo   ------------------------------------
echo.
rem The built program carries everything it needs, so prefer it.
if exist "VocabTrainer.exe" (
  echo   Starting VocabTrainer.exe, no Python needed.
  start "" "VocabTrainer.exe"
  popd
  exit /b
)
if exist "VocabTrainerSetup.exe" (
  echo   Running the installer, which needs no Python.
  start "" "VocabTrainerSetup.exe"
  popd
  exit /b
)
if not exist "BengaliEnglishTrainer.py" (
  echo   Nothing to run in this folder.
  echo   Keep this file next to VocabTrainer.exe or BengaliEnglishTrainer.py.
  pause
  popd
  exit /b 1
)
set "PY="
py -3 -c "pass" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "pass" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  python3 -c "pass" >nul 2>&1
  if not errorlevel 1 set "PY=python3"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python" (
    for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do (
      if not defined PY set "PY=%%P"
    )
  )
)
if not defined PY (
  echo   Python was not found on this computer.
  echo.
  echo   You do not need it. Run VocabTrainerSetup.exe instead: it installs
  echo   the ready built program and sets up nothing else.
  echo.
  echo   To run from the source anyway, get Python from
  echo   https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  popd
  exit /b 1
)
echo   Using: %PY%
echo   Installing anything that is missing, please wait...
echo.
%PY% -m pip install --disable-pip-version-check --upgrade pystray pillow
echo.
echo   Starting the app. Look for the blue icon near the clock.
%PY% "BengaliEnglishTrainer.py"
echo.
echo   The app has closed. Any error is shown above.
popd
pause
