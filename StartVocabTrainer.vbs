Option Explicit
Dim shell, fso, here, exe, target
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
target = here & "\BengaliEnglishTrainer.py"
exe = "pythonw"
On Error Resume Next
shell.Run """" & exe & """ """ & target & """", 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "pyw -3 """ & target & """", 0, False
    If Err.Number <> 0 Then
        Err.Clear
        shell.Run "python """ & target & """", 0, False
    End If
End If
