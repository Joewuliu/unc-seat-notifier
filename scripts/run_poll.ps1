<#
.SYNOPSIS
    Production launcher for unc-seat-notifier's `poll` command. Intended to be
    invoked by Windows Task Scheduler on a fixed interval (see README.md).

.DESCRIPTION
    Resolves the repository root from this script's own location - never from the
    caller's current working directory, so it works no matter what "Start in"
    folder Task Scheduler uses (or omits). Invokes the virtual environment's
    Python directly with an explicit --env-file, appends stdout/stderr to a
    dated log file under logs\, and exits with the same code the Python process
    returned so Task Scheduler can see success/failure.

.PARAMETER DryRun
    Print the resolved paths as JSON and exit 0 without touching Python, the venv,
    or the log file. Lets you (or a test) verify path resolution is correct without
    requiring a real virtual environment or .env to exist.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_poll.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_poll.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$EnvFile = Join-Path $RepoRoot ".env"
$LogDir = Join-Path $RepoRoot "logs"
$LogFile = Join-Path $LogDir ("poll_{0:yyyyMMdd}.log" -f (Get-Date))

if ($DryRun) {
    [PSCustomObject]@{
        RepoRoot   = $RepoRoot
        VenvPython = $VenvPython
        EnvFile    = $EnvFile
        LogDir     = $LogDir
        LogFile    = $LogFile
    } | ConvertTo-Json
    exit 0
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Error -ErrorAction Continue (
        "Virtual environment Python not found at $VenvPython - " +
        "create it with 'python -m venv .venv' and install dependencies in $RepoRoot first."
    )
    exit 1
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    Write-Error -ErrorAction Continue (
        ".env not found at $EnvFile - copy .env.example to .env and fill it in first."
    )
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Add-Content -LiteralPath $LogFile -Encoding utf8 -Value (
    "===== poll run started $((Get-Date).ToString('o')) ====="
)

# Redirecting a native command's stderr with $ErrorActionPreference = "Stop" makes
# PowerShell treat the first stderr line as a terminating error - even though it's
# simultaneously being redirected to a file - which would abort this script right in
# the middle of the child's own output, before the "ended" marker below ever runs.
# Only the invocation itself needs the relaxed preference.
$ErrorActionPreference = "Continue"
& $VenvPython -m unc_notifier poll --env-file $EnvFile *>> $LogFile
$exitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

Add-Content -LiteralPath $LogFile -Encoding utf8 -Value (
    "===== poll run ended $((Get-Date).ToString('o')) exit=$exitCode ====="
)

exit $exitCode
