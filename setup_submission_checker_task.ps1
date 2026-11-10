# ==============================================================================
# setup_submission_checker_task.ps1
# Windows Task Scheduler LETUS Submission Checker Registration
# (Pure ASCII path: launches pythonw.exe directly from local SSD)
# ==============================================================================

$localApp = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::LocalApplicationData)
$taskRadarDir = Join-Path $localApp "TaskRadar"
if (-not (Test-Path $taskRadarDir)) {
    New-Item -ItemType Directory -Path $taskRadarDir -Force | Out-Null
}

$workspaceDir = $PSScriptRoot
if (-not $workspaceDir) {
    $workspaceDir = "c:\Users\fujit\Box\01学業2026年度\20"
}

# 1. Copy runner script to local SSD (pure ASCII path)
$srcRunner = Join-Path $workspaceDir "run_letus_task.py"
$destRunner = Join-Path $taskRadarDir "run_letus_task.py"
if (Test-Path $srcRunner) {
    Copy-Item $srcRunner $destRunner -Force
}

$pythonw = Join-Path $localApp "Programs\Python\Python313\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = "pythonw.exe"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# ------------------------------------------------------------------------------
# 1. Morning Smart Checker (Login + 3min delay)
# ------------------------------------------------------------------------------
$morningTaskName = "LetusMorningSubmissionChecker"
$morningAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$destRunner`" --morning"
$morningTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$morningTrigger.Delay = 'PT3M'

Unregister-ScheduledTask -TaskName $morningTaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $morningTaskName `
    -Action $morningAction `
    -Trigger $morningTrigger `
    -Settings $settings `
    -Description "LETUS Morning Smart Submission Checker"

Write-Host "[OK] Registered: $morningTaskName"

# ------------------------------------------------------------------------------
# 2. Evening Checker (Daily 20:00)
# ------------------------------------------------------------------------------
$eveningTaskName = "LetusEveningSubmissionChecker"
$eveningAction = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$destRunner`" --evening"
$eveningTrigger = New-ScheduledTaskTrigger -Daily -At "20:00:00"

Unregister-ScheduledTask -TaskName $eveningTaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $eveningTaskName `
    -Action $eveningAction `
    -Trigger $eveningTrigger `
    -Settings $settings `
    -Description "LETUS Evening Daily Submission Checker"

Write-Host "[OK] Registered: $eveningTaskName"
