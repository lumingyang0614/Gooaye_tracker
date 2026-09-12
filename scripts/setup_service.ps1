# Registers Gooaye Tracker as a resident service via Windows Task Scheduler:
# starts at boot, before anyone logs in, and restarts itself if it dies.
#
#     powershell -ExecutionPolicy Bypass -File scripts\setup_service.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\setup_service.ps1 -Port 8100
#     powershell -ExecutionPolicy Bypass -File scripts\setup_service.ps1 -BindHost 0.0.0.0
#
# Must be run from an elevated PowerShell: registering a task that runs as
# SYSTEM at boot requires administrator rights.
#
# This is the right choice for a machine that should keep working on its own,
# and it supersedes setup_schedule.ps1 rather than complementing it — see the
# warning that script now prints. Running both means two processes syncing the
# same episodes into the same database at overlapping times.
#
# To remove it: scripts\remove_service.ps1

param(
    [int]$Port = 8100,
    [string]$BindHost = "127.0.0.1",
    [string]$TaskName = "GooayeTrackerService"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot "run_server.ps1"

if (-not (Test-Path $RunScript)) {
    Write-Error ("Cannot find " + $RunScript + " - run this script from inside the project.")
    exit 1
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this from an elevated PowerShell (Run as administrator)."
    exit 1
}

if ($BindHost -ne "127.0.0.1") {
    Write-Warning ("Binding to " + $BindHost + " exposes the dashboard and its API to the network. " +
        "There is no authentication in front of it, so only do this on a trusted network " +
        "or behind a reverse proxy that adds one.")
}

$ActionArg = '-NoProfile -ExecutionPolicy Bypass -File "' + $RunScript + '" -Port ' + $Port + ' -BindHost ' + $BindHost

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArg -WorkingDirectory $ProjectRoot

# AtStartup so it comes back after a reboot without anyone logging in.
$Trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM, so it does not depend on a user session staying open. The project
# reads its secrets from .env in the project directory rather than from the
# user's environment, so it does not need a particular user's profile.
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# ExecutionTimeLimit 0 means "no limit" — this task is meant to never finish,
# and the default 3-day limit would otherwise kill it silently.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Gooaye Tracker: resident API and background scheduler (episode sync, securities master, merged company views)." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Output ("Registered and started '" + $TaskName + "' (http://" + $BindHost + ":" + $Port + ").")
Write-Output ""
Write-Output "Check it is alive:"
Write-Output ("  Get-ScheduledTask -TaskName '" + $TaskName + "' | Get-ScheduledTaskInfo")
Write-Output ("  Invoke-RestMethod http://127.0.0.1:" + $Port + "/api/schedules/status | ConvertTo-Json -Depth 4")
Write-Output ""
Write-Output ("Logs: " + (Join-Path $ProjectRoot "logs\server.log"))
Write-Output ("Stop/remove: powershell -ExecutionPolicy Bypass -File scripts\remove_service.ps1")

$conflict = Get-ScheduledTask -TaskName "GooayeTrackerSync" -ErrorAction SilentlyContinue
if ($conflict) {
    Write-Warning ("The older 'GooayeTrackerSync' task is also registered. It runs its own sync pass, " +
        "so both would analyze the same episodes at overlapping times. Remove it with: " +
        "powershell -ExecutionPolicy Bypass -File scripts\remove_schedule.ps1")
}
