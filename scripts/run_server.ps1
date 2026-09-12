# Runs the Gooaye Tracker service in the foreground, logging to logs\server.log.
# This is what setup_service.ps1 registers with Task Scheduler; run it directly
# to see whether the service starts cleanly before registering anything.
#
#     powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1 -Port 8100
#
# Starting the API is what makes the project resident: app/main.py's lifespan
# starts app/services/background_updates.py, which owns the three loops
# (episode_sync, taiwan_securities, merge_refresh). Nothing is periodic without
# this process running — that is the whole difference from setup_schedule.ps1,
# which instead fires one sync pass at a time and never runs merge_refresh.

param(
    [int]$Port = 8100,
    [string]$BindHost = "127.0.0.1",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

. (Join-Path $PSScriptRoot "_common.ps1")
$PythonExe = Get-ProjectPython -ProjectRoot $ProjectRoot -PythonExe $PythonExe

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "server.log"

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value ("`n[" + $startedAt + "] starting uvicorn on " + $BindHost + ":" + $Port + " using " + $PythonExe)

if (-not (Assert-ProjectPython -PythonExe $PythonExe -ProjectRoot $ProjectRoot -LogFile $LogFile)) { exit 1 }

# Redirection is handed to cmd.exe rather than written as PowerShell's `*>>`.
# uvicorn logs to stderr, and Windows PowerShell wraps each stderr line from a
# native command in an ErrorRecord — which, under the $ErrorActionPreference
# above, is terminating. The launcher therefore died on uvicorn's first log
# line, leaving a log containing only the header line above and no server.
#
# No --reload: the reloader spawns a child process, which makes the task's
# process tree ambiguous to stop and can run the scheduler twice.
$command = '"{0}" -m uvicorn app.main:app --host {1} --port {2} >> "{3}" 2>&1' -f $PythonExe, $BindHost, $Port, $LogFile
& cmd.exe /c $command
$exitCode = $LASTEXITCODE

$finishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value ("[" + $finishedAt + "] uvicorn exited (exit code " + $exitCode + ")")
exit $exitCode
