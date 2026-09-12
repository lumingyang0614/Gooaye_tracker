# Stops and unregisters the resident Gooaye Tracker service.
#
#     powershell -ExecutionPolicy Bypass -File scripts\remove_service.ps1
#
# Run from an elevated PowerShell, for the same reason setup_service.ps1 needs
# it: the task runs as SYSTEM. Nothing in the database is touched.

param([string]$TaskName = "GooayeTrackerService")

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output ("No scheduled task named '" + $TaskName + "' - nothing to remove.")
    exit 0
}

# Stopping first so the running uvicorn goes away with the task, rather than
# being orphaned and continuing to hold the port.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Output ("Removed '" + $TaskName + "'. The API is no longer resident, so new episodes are only")
Write-Output "picked up when you run a sync yourself: python -m scripts.sync_gooaye"
