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

    REPETITION DURATION: repeating a trigger "every 5 minutes, forever" is expressed
    to Task Scheduler by setting RepetitionInterval and leaving RepetitionDuration
    unset entirely - Microsoft's own documentation states this means the repetition
    never ends, and building the equivalent trigger directly against the Task
    Scheduler COM API (Schedule.Service - no elevation needed, since it only builds
    an in-memory task definition and never calls the service) confirms the resulting
    XML has no <Duration> element under <Repetition> at all, which is the schema-
    correct "repeat indefinitely" shape. An earlier version of this script instead
    passed -RepetitionDuration ([TimeSpan]::MaxValue), intending the same thing -
    but that serializes to the ISO-8601 duration "P99999999DT23H59M59S", which the
    live Task Scheduler *service* rejects at registration time with "The task XML
    contains a value which is incorrectly formatted or out of range" (HRESULT
    0x80041318, confirmed against a real Windows installation). Omitting the
    parameter avoids ever producing that value.

    -RepetitionYears is an optional escape hatch, off by default, in case a future
    Windows version or policy ever requires an explicit (non-omitted) Duration for
    repetition to take effect: it sets a finite Duration of that many years instead
    of omitting it. Unlike the default (which never expires), a finite Duration
    means the trigger stops firing new repetitions once that many years have
    elapsed since registration (StartBoundary) - Task Scheduler does not
    auto-renew it. You would need to re-run this script with -ConfirmRegister
    again before that point to keep polling going. This has not been verified
    against a live, elevated registration (unavailable in this environment) - it's
    a documented fallback, not the recommended path.

.PARAMETER ConfirmRegister
    Required to actually register the task. Without it, this script only previews
    and never calls Register-ScheduledTask.

.PARAMETER TaskName
    Defaults to "UNC Seat Notifier". Overridable so tests (or a side-by-side trial
    registration) can use a distinct name without colliding with the real task.

.PARAMETER RepetitionYears
    Defaults to 0, meaning RepetitionDuration is omitted entirely (repeats every 5
    minutes indefinitely - the recommended, documented behavior; see DESCRIPTION).
    If set to a positive number, uses an explicit finite Duration of that many
    years instead - see DESCRIPTION for why you might need this and its renewal
    implications. Never accepts a value large enough to reproduce the original
    out-of-range failure.

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
    [string]$TaskName = "UNC Seat Notifier",
    [ValidateRange(0, 100)]
    [int]$RepetitionYears = 0
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

if ($RepetitionYears -gt 0) {
    # Documented fallback only - see DESCRIPTION. A finite Duration this large is
    # nowhere near the ~99999999-day value that caused the original failure.
    $repetitionDuration = New-TimeSpan -Days ($RepetitionYears * 365)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration $repetitionDuration
} else {
    # Recommended: omitting -RepetitionDuration entirely is Task Scheduler's
    # documented, schema-correct way to repeat every 5 minutes indefinitely - see
    # DESCRIPTION for why the previous -RepetitionDuration ([TimeSpan]::MaxValue)
    # was wrong.
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
}

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
if ([string]::IsNullOrEmpty($trigger.Repetition.Duration)) {
    $durationDescription = "no repetition end (Duration intentionally omitted - Task Scheduler's documented way to repeat forever)"
} else {
    $durationDescription = "stops repeating after Duration=$($trigger.Repetition.Duration) from registration time - re-register before then to keep polling going"
}
Write-Host ("{0,-24}: {1}" -f "Trigger", "Repeats every $($trigger.Repetition.Interval) (5 minutes), starting now - $durationDescription")
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
