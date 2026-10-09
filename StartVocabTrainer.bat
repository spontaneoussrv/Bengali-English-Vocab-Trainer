@echo off
pushd "%~dp0"
if errorlevel 1 exit /b 1
if exist "VocabTrainer.exe" (
  start "" "VocabTrainer.exe"
  popd
  exit /b
)
if exist "StartVocabTrainer.vbs" (
  start "" wscript.exe "StartVocabTrainer.vbs"
  popd
  exit /b
)
start "" pythonw "BengaliEnglishTrainer.py"
popd
