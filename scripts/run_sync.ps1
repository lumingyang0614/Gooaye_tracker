# Runs one Gooaye sync pass and appends timestamped output to logs\sync.log.
# This is the script Windows Task Scheduler actually invokes — see setup_schedule.ps1.

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "sync.log"

. (Join-Path $PSScriptRoot "_common.ps1")
$PythonExe = Get-ProjectPython -ProjectRoot $ProjectRoot

$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value ("`n[" + $startedAt + "] sync_gooaye started")

try {
    # Redirection via cmd.exe, not PowerShell's `*>>`: Python logging writes to
    # stderr, and Windows PowerShell turns a native command's stderr into
    # terminating ErrorRecords under the $ErrorActionPreference above. The
    # catch below would then report a "launcher error" for a sync that had in
    # fact started correctly, and the real output never reached the log.
    $command = '"{0}" -m scripts.sync_gooaye >> "{1}" 2>&1' -f $PythonExe, $LogFile
    & cmd.exe /c $command
    $exitCode = $LASTEXITCODE
}
catch {
    Add-Content -Path $LogFile -Value ("sync_gooaye launcher error: " + $_.Exception.Message)
    $exitCode = 1
}

$finishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value ("[" + $finishedAt + "] sync_gooaye finished (exit code " + $exitCode + ")")
exit $exitCode
