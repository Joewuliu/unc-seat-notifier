<#
.SYNOPSIS
    Previews, and optionally registers, the "UNC Seat Notifier" Windows Task
    Scheduler task that runs scripts\run_poll.ps1 every 5 minutes.

.DESCRIPTION
    Default (no -ConfirmRegister) is PREVIEW ONLY: builds the exact task-scheduler
    action/trigger/settings/principal objects this script would register, prints
    every setting from those actual objects (not a separate hand-written
    description that could drift out of sync), and exits without calling
    Register-ScheduledTask at all - Task Scheduler is never touched in preview mode.

    Uses the ScheduledTasks PowerShell module (Register-ScheduledTask and friends),
    not schtasks.exe: several required settings here - execution time limit,
    restart count/interval, "start as soon as possible after a missed start",
    running on battery - aren't expressible through schtasks.exe's /Create flags,
    but map directly onto New-ScheduledTaskSettingsSet parameters.

    Registering (with -ConfirmRegister) as the recommended SYSTEM account normally
    requires this script to be run from an elevated ("Run as Administrator")
    PowerShell session - Register-ScheduledTask fails with "Access is denied"
    otherwise. SYSTEM has no interactive password, so Windows does not prompt for
    one. It would only prompt for a password if you registered the task under a
    real user account instead (not what this script does by default).

.PARAMETER ConfirmRegister
    Required to actually register the task. Without it, this script only previews
    and never calls Register-ScheduledTask.

.PARAMETER TaskName
    Defaults to "UNC Seat Notifier". Overridable so tests (or a side-by-side trial
    registration) can use a distinct name without colliding with the real task.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1
    # preview only - prints every setting, registers nothing

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_task.ps1 -ConfirmRegister
    # registers for real (run this from an elevated PowerShell session)
#>

[CmdletBinding()]
param(
    [switch]$ConfirmRegister,
    [string]$TaskName = "UNC Seat Notifier"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Launcher = Join-Path $RepoRoot "scripts\run_poll.ps1"
$LogDir = Join-Path $RepoRoot "logs"

if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    Write-Error -ErrorAction Continue "Launcher not found at $Launcher - nothing to register."
    exit 1
}

$powerShellExe = (Get-Command powershell.exe).Source
$launcherArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""

# Single source of truth: these are the exact objects that get passed to
# Register-ScheduledTask below when -ConfirmRegister is given. Preview mode prints
# their real properties, not a hand-written paraphrase that could drift.
$action = New-ScheduledTaskAction -Execute $powerShellExe -Argument $launcherArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Limited

Write-Host "==================== UNC Seat Notifier: Task Scheduler registration ===================="
Write-Host ("{0,-24}: {1}" -f "Task name", $TaskName)
Write-Host ("{0,-24}: {1}" -f "Program", $action.Execute)
Write-Host ("{0,-24}: {1}" -f "Arguments", $action.Arguments)
Write-Host ("{0,-24}: {1}" -f "Start in", $action.WorkingDirectory)
Write-Host ("{0,-24}: {1}" -f "Trigger", "Repeats every $($trigger.Repetition.Interval) (5 minutes), starting now, indefinitely")
Write-Host ("{0,-24}: {1}" -f "User account context", "$($principal.UserId) (LogonType=$($principal.LogonType) - no stored password needed)")
Write-Host ("{0,-24}: {1}" -f "Run level", $principal.RunLevel)
Write-Host ("{0,-24}: {1}" -f "Overlap behavior", "MultipleInstances=$($settings.MultipleInstances) (do not start a new instance if one is already running) - belt-and-suspenders alongside poll's own single_instance_lock()")
Write-Host ("{0,-24}: {1}" -f "Maximum runtime", "ExecutionTimeLimit=$($settings.ExecutionTimeLimit) (4 minutes) - a poll normally finishes in seconds")
Write-Host ("{0,-24}: {1}" -f "Restart on failure", "RestartCount=$($settings.RestartCount), RestartInterval=$($settings.RestartInterval) (up to 3 times, 1 minute apart)")
Write-Host ("{0,-24}: {1}" -f "Start when available", "$($settings.StartWhenAvailable) (runs ASAP if a scheduled start was missed, e.g. the computer was off)")
Write-Host ("{0,-24}: {1}" -f "Runs on battery", "$(-not $settings.DisallowStartIfOnBatteries) (does not stop if switching to battery either)")
Write-Host ("{0,-24}: {1}" -f "Wakes the computer", "$($settings.WakeToRun)")
Write-Host ("{0,-24}: {1}" -f "Launcher log", "$LogDir\poll_<date>.log (raw stdout/stderr) and $LogDir\unc_notifier.log (structured)")
Write-Host ("{0,-24}: {1}" -f "Verify", "scripts\verify_task.ps1 -TaskName '$TaskName'")
Write-Host ("{0,-24}: {1}" -f "Disable", "Disable-ScheduledTask -TaskName '$TaskName'")
Write-Host ("{0,-24}: {1}" -f "Remove", "scripts\remove_task.ps1 -TaskName '$TaskName' -ConfirmRemove")
Write-Host "==========================================================================================="

if (-not $ConfirmRegister) {
    Write-Host ""
    Write-Host "PREVIEW ONLY - no task was registered. Re-run with -ConfirmRegister to register for real."
    exit 0
}

Write-Host ""
Write-Host "Registering now (requires an elevated / 'Run as Administrator' PowerShell session for the"
Write-Host "SYSTEM account context above - Windows will not prompt for a password for SYSTEM, since it"
Write-Host "has none; a real user account would prompt for one instead, which this script does not use)."

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered '$TaskName'. Verify with: scripts\verify_task.ps1 -TaskName '$TaskName'"
