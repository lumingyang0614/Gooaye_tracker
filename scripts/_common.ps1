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

        Discovered rather than guessed from a fixed list of names. The venv is
        called .venv on one machine, venv_Gooaye on another and
        venv_gooaye_tracker on a third; a hard-coded list silently fell through
        to the system Python on the machine whose name was not in it, and the
        only symptom was the scheduled task exiting with code 1.

        Pass -PythonExe to override when the interpreter lives outside the
        project directory (a conda env, a system-wide install).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$PythonExe = ""
    )

    if ($PythonExe) { return $PythonExe }

    # Any virtualenv directly inside the project, most recently created first,
    # so a freshly rebuilt environment wins over an abandoned one.
    # @() matters: a pipeline yielding one item returns a string, not an array,
    # and $found[0] on a string is its first character — which produced the
    # interpreter path "F" from "F:\Gooaye\venv_Gooaye\Scripts\python.exe".
    $found = @(
        Get-ChildItem -Path $ProjectRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "Scripts\python.exe" } |
            Where-Object { Test-Path $_ } |
            Sort-Object { (Get-Item $_).LastWriteTime } -Descending
    )

    if ($found.Count -gt 0) { return $found[0] }
    return "python"
}

function Assert-ProjectPython {
    <#
    .SYNOPSIS
        Fail loudly, in the log, when the interpreter cannot run this project.

    .DESCRIPTION
        Without this the launcher just exits non-zero and Task Scheduler shows
        LastTaskResult 1 with nothing to explain it — the import error goes to
        a stderr that nobody captured yet. Checked before the long-running
        command starts so the message lands at the top of the log.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$LogFile
    )

    # Continue, not the caller's Stop: a failing probe writes an ImportError to
    # stderr, which Windows PowerShell turns into a terminating ErrorRecord for
    # a native command — aborting before the diagnosis below could be written.
    # That is the same trap that made the launcher die on uvicorn's first log
    # line, and the probe exists precisely to run when things are broken.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $probe = & $PythonExe -c "import sqlalchemy, fastapi, uvicorn; print('ok')" 2>&1
        $code = $LASTEXITCODE
    }
    catch { $probe = $_.Exception.Message; $code = 1 }
    finally { $ErrorActionPreference = $previous }

    if ($code -eq 0 -and ($probe -join "") -match "ok") { return $true }

    $message = @(
        "FATAL: " + $PythonExe + " cannot run this project.",
        "  " + (($probe | Select-Object -Last 3) -join "`r`n  "),
        "  Create the virtualenv and install requirements, or pass -PythonExe with the full path:",
        "      python -m venv .venv",
        "      .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    ) -join "`r`n"

    Add-Content -Path $LogFile -Value $message
    Write-Error $message
    return $false
}
