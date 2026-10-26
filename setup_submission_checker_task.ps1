# ==============================================================================
# setup_submission_checker_task.ps1
# Windowsタスクスケジューラに LETUS実提出チェッカー（朝一番＆夜20時）を自動登録
# ==============================================================================

$baseDir = "c:\Users\fujit\Box\01学業2026年度\20"
$wscript = "$env:SystemRoot\System32\wscript.exe"
$morningVbs = Join-Path $baseDir "check_morning_submission.vbs"
$eveningVbs = Join-Path $baseDir "check_letus_submissions.vbs"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# ------------------------------------------------------------------------------
# 1. 朝一番スマートチェッカー (PC起動・ログイン時の初回のみ実行)
# ------------------------------------------------------------------------------
$morningTaskName = "LetusMorningSubmissionChecker"
$morningAction = New-ScheduledTaskAction -Execute $wscript -Argument "`"$morningVbs`"" -WorkingDirectory $baseDir
$morningTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$morningTrigger.Delay = 'PT1M' # ログイン後1分待機して負荷分散

Unregister-ScheduledTask -TaskName $morningTaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $morningTaskName `
    -Action $morningAction `
    -Trigger $morningTrigger `
    -Settings $settings `
    -Description "朝(06:00-12:00)のPCログイン初回のみ、LETUS実提出状況を静かに点検するスマートチェッカー"

Write-Host "✅ 登録完了: $morningTaskName (朝一番PCオープン時)"

# ------------------------------------------------------------------------------
# 2. 毎晩20:00 夜の締切前チェッカー (毎日 20:00 定期実行)
# ------------------------------------------------------------------------------
$eveningTaskName = "LetusEveningSubmissionChecker"
$eveningAction = New-ScheduledTaskAction -Execute $wscript -Argument "`"$eveningVbs`"" -WorkingDirectory $baseDir
$eveningTrigger = New-ScheduledTaskTrigger -Daily -At "20:00:00"

Unregister-ScheduledTask -TaskName $eveningTaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $eveningTaskName `
    -Action $eveningAction `
    -Trigger $eveningTrigger `
    -Settings $settings `
    -Description "毎晩20:00にLETUS実提出状況を静かに点検し、提出完了の反映＆誤完了の警告復元を行うチェッカー"

Write-Host "✅ 登録完了: $eveningTaskName (毎日 20:00 定期実行)"
