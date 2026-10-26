' 夜20時 定期実提出チェッカー (完全透明・黒画面なし)
' LETUSの実提出状況を自動確認し、提出済みを完了に昇格＆誤完了があれば警告復元します
Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "python """ & scriptDir & "\scan_letus_status.py"" --headless"
WshShell.Run cmd, 0, False
