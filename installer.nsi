Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APPNAME "Bengali to English Vocabulary Trainer"
!define SHORTNAME "VocabTrainer"
!define COMPANY "SheetClub"
!define VERSIONTEXT "1.2"

Name "${APPNAME}"
OutFile "VocabTrainerSetup.exe"
; per user install, so there is no administrator prompt and no extra click
InstallDir "$LOCALAPPDATA\Programs\${SHORTNAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails hide
AutoCloseWindow true

VIProductVersion "1.2.0.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} setup"
VIAddVersionKey "FileVersion" "${VERSIONTEXT}"
VIAddVersionKey "LegalCopyright" "${COMPANY}"

!define MUI_ICON "VocabTrainer.ico"
!define MUI_UNICON "VocabTrainer.ico"

; one click: a single progress page, then the app starts by itself
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File "VocabTrainer.exe"
  File "VocabTrainer.ico"
  File "README.md"
  File "BengaliEnglishTrainer.py"

  CreateShortCut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\VocabTrainer.exe" "" \
    "$INSTDIR\VocabTrainer.ico"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\VocabTrainer.exe" "" \
    "$INSTDIR\VocabTrainer.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" \
    "BengaliVocabTrainer" "$\"$INSTDIR\VocabTrainer.exe$\""

  WriteRegStr HKCU "Software\${SHORTNAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "DisplayIcon" "$INSTDIR\VocabTrainer.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "DisplayVersion" "${VERSIONTEXT}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "Publisher" "${COMPANY}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}" \
    "EstimatedSize" "$0"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Function .onInstSuccess
  ; start it straight away, so one click really is all it takes
  Exec '"$INSTDIR\VocabTrainer.exe"'
FunctionEnd

Section "Uninstall"
  ; stop it first, otherwise the exe cannot be replaced or removed
  ExecWait 'taskkill /f /im VocabTrainer.exe'
  Delete "$INSTDIR\VocabTrainer.exe"
  Delete "$INSTDIR\VocabTrainer.ico"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\BengaliEnglishTrainer.py"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$DESKTOP\${APPNAME}.lnk"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "BengaliVocabTrainer"
  DeleteRegKey HKCU "Software\${SHORTNAME}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SHORTNAME}"

  IfSilent keepdata
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "Keep your saved words, favourites and progress?$\n$\nChoose No to delete them as well." \
    IDYES keepdata
  RMDir /r "$APPDATA\BengaliVocabTrainer"
  keepdata:
SectionEnd
