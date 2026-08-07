<#
.SYNOPSIS
    Read-only status report for the "UNC Seat Notifier" Windows Task Scheduler task.

.DESCRIPTION
    Never registers, removes, disables, or otherwise modifies the task - purely
    inspects it via Get-ScheduledTask/Get-ScheduledTaskInfo. Safe to run any time,
    as often as you like, whether or not the task exists yet.

.PARAMETER TaskName
    Defaults to "UNC Seat Notifier".

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_task.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "UNC Seat Notifier"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $RepoRoot "logs"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "No scheduled task named '$TaskName' exists."
    Write-Host "Register it with: scripts\register_task.ps1 -ConfirmRegister"
    exit 1
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host "Task name            : $($task.TaskName)"
Write-Host "Current running state: $($task.State)"
Write-Host "Enabled              : $($task.Settings.Enabled)"
Write-Host "Last run time        : $($info.LastRunTime)"
Write-Host "Last result          : $($info.LastTaskResult) (0 = success)"
Write-Host "Next run time        : $($info.NextRunTime)"
Write-Host "Missed run count     : $($info.NumberOfMissedRuns)"

$latestLog = Get-ChildItem -Path $LogDir -Filter "poll_*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -ne $latestLog) {
    Write-Host "Latest launcher log  : $($latestLog.FullName)"
} else {
    Write-Host "Latest launcher log  : none found under $LogDir"
}
Write-Host "Structured app log   : $LogDir\unc_notifier.log"
