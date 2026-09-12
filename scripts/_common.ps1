# Shared helpers for the PowerShell entry points. Dot-source it:
#     . (Join-Path $PSScriptRoot "_common.ps1")

function Get-ProjectPython {
    <#
    .SYNOPSIS
        The interpreter that has this project's dependencies installed.

    .DESCRIPTION
        Task Scheduler starts with a clean environment, so no virtualenv is
        active and a bare "python" is whatever is first on the machine's PATH —
        usually a system Python without sqlalchemy, fastapi or faster-whisper.
        The failure is not obvious either: the task reports a non-zero exit and
        the traceback only appears inside the log file.

        Looked at in order, because the venv directory is not named
        consistently across the machines this runs on (.venv here,
        venv_Gooaye there).
    #>
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    foreach ($name in @(".venv", "venv_Gooaye", "venv", "env")) {
        $candidate = Join-Path $ProjectRoot "$name\Scripts\python.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return "python"
}
