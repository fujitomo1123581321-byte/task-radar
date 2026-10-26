' 朝一番スマート実提出チェッカー (完全透明・黒画面なし)
' 午前中(06:00〜12:00)のPC起動初回のみ実行され、2回目以降や昼以降は即座に終了します
Set WshShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "python """ & scriptDir & "\scan_letus_status.py"" --headless --morning-check-only"
WshShell.Run cmd, 0, False
