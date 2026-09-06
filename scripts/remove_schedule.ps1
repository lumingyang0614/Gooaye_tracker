# Removes the scheduled task created by setup_schedule.ps1.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\remove_schedule.ps1

param(
    [string]$TaskName = "GooayeTrackerSync"
)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Output ("No scheduled task named '" + $TaskName + "' was found; nothing to remove.")
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output ("Removed scheduled task '" + $TaskName + "'.")
