# Runs one Gooaye sync pass and appends timestamped output to logs\sync.log.
# This is the script Windows Task Scheduler actually invokes — see setup_schedule.ps1.

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "sync.log"

$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) { $PythonExe = "python" }

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value ("`n[" + $startedAt + "] sync_gooaye started")

try {
    & $PythonExe -m scripts.sync_gooaye *>> $LogFile
    $exitCode = $LASTEXITCODE
}
catch {
    Add-Content -Path $LogFile -Value ("sync_gooaye launcher error: " + $_.Exception.Message)
    $exitCode = 1
}

$finishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value ("[" + $finishedAt + "] sync_gooaye finished (exit code " + $exitCode + ")")
exit $exitCode
