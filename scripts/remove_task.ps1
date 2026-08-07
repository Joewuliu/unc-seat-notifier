<#
.SYNOPSIS
    Removes (unregisters) the "UNC Seat Notifier" Windows Task Scheduler task.

.DESCRIPTION
    Default (no -ConfirmRemove) is PREVIEW ONLY: reports whether the task exists
    and exits without removing anything. Removal only happens with -ConfirmRemove.

    Removing the task never touches run_poll.ps1, .env, watches.yaml, or
    data\unc_notifier.db - only the Task Scheduler registration itself. Re-running
    scripts\register_task.ps1 -ConfirmRegister later picks up exactly where it left
    off.

.PARAMETER ConfirmRemove
    Required to actually remove the task. Without it, this script only reports
    whether the task exists and never calls Unregister-ScheduledTask.

.PARAMETER TaskName
    Defaults to "UNC Seat Notifier".

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remove_task.ps1
    # preview only - reports state, removes nothing

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\remove_task.ps1 -ConfirmRemove
    # removes the task for real (requires the same elevation registration needed)
#>

[CmdletBinding()]
param(
    [switch]$ConfirmRemove,
    [string]$TaskName = "UNC Seat Notifier"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($null -eq $task) {
    Write-Host "No scheduled task named '$TaskName' exists - nothing to remove."
    exit 0
}

Write-Host "Found task '$TaskName' (State: $($task.State))."
Write-Host "Removing it does NOT touch run_poll.ps1, .env, watches.yaml, or data\unc_notifier.db."

if (-not $ConfirmRemove) {
    Write-Host ""
    Write-Host "PREVIEW ONLY - no task was removed. Re-run with -ConfirmRemove to remove it for real."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
