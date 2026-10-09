@echo off
setlocal
title Vocab Trainer check
pushd "%~dp0"
if errorlevel 1 (
  echo   Could not open this folder. Extract the zip first.
  pause
  exit /b 1
)
if exist "VocabTrainer.exe" (
  "VocabTrainer.exe" --doctor
  echo.
  pause
  popd
  exit /b
)
set "PY="
py -3 -c "pass" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python -c "pass" >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo   Neither VocabTrainer.exe nor Python is here.
  echo   Run VocabTrainerSetup.exe to install the ready built program.
  pause
  popd
  exit /b 1
)
%PY% "BengaliEnglishTrainer.py" --doctor
echo.
pause
popd
