Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
pyExe = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe"
If Not FSO.FileExists(pyExe) Then
    pyExe = "pythonw"
End If
cmd = """" & pyExe & """ """ & scriptDir & "\scan_letus_status.py"" --headless"
WshShell.Run cmd, 0, False
