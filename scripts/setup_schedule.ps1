# Registers a Windows Task Scheduler task that fires one Gooaye Tracker sync
# pass at a time.
#
# This predates the project having a scheduler of its own, and that comment is
# no longer true: app/services/background_updates.py now runs three loops
# (episode_sync, taiwan_securities, merge_refresh) inside the API process.
# Prefer scripts\setup_service.ps1, which keeps that process resident — it also
# covers merge_refresh, which this path never runs, so merged company views go
# stale whenever X Stock Tracker updates its own rows.
#
# Use this one only when you specifically do not want a long-running process.
# Do not run both: they would analyze the same episodes at overlapping times.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -IntervalHours 4
#
# Re-running this with the same -TaskName updates the existing task in place.
# To remove it later: scripts\remove_schedule.ps1

param(
    [int]$IntervalHours = 6,
    [string]$TaskName = "GooayeTrackerSync"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot "run_sync.ps1"

if (-not (Test-Path $RunScript)) {
    Write-Error ("Cannot find " + $RunScript + " - run this script from inside the project.")
    exit 1
}

$ActionArg = '-NoProfile -ExecutionPolicy Bypass -File "' + $RunScript + '"'

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArg -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
# Deliberately no -RepetitionDuration: a -Once trigger with only
# -RepetitionInterval set repeats indefinitely. Task Scheduler's XML schema
# rejects an explicit "forever" duration like [TimeSpan]::MaxValue.

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

$TaskDescription = [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::UTF8.GetBytes("Gooaye Tracker: periodic sync of new episodes, LLM analysis, and company insights."))

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description $TaskDescription `
    -Force | Out-Null

Write-Output ("Scheduled task '" + $TaskName + "' created: runs 'python -m scripts.sync_gooaye' every " + $IntervalHours + " hour(s).")
Write-Output "First run fires in 2 minutes. Check status with: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Output ("Logs are appended to: " + (Join-Path $ProjectRoot "logs\sync.log"))
Write-Output "To remove it: powershell -ExecutionPolicy Bypass -File scripts\remove_schedule.ps1"
