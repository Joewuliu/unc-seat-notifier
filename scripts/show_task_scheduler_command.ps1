<#
.SYNOPSIS
    Prints the exact `schtasks /create` command that would register unc-seat-notifier's
    poll task in Windows Task Scheduler - and does NOT run it.

.DESCRIPTION
    This script only builds and prints the command as text. Registering the task is a
    separate, explicit, human decision - see README.md's "Task Scheduler setup"
    section for how to review and run the printed command yourself (or use Task
    Scheduler's GUI with the same values).
#>

[CmdletBinding()]
param()

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Launcher = Join-Path $RepoRoot "scripts\run_poll.ps1"

$schtasksArgs = @(
    '/Create'
    '/TN "UNC Seat Notifier"'
    '/TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"' + $Launcher + '`""'
    '/SC MINUTE'
    '/MO 5'
    '/RL LIMITED'
    '/RU SYSTEM'
    '/F'
)

Write-Host "Repository root: $RepoRoot"
Write-Host "Launcher script: $Launcher"
Write-Host ""
Write-Host "This script does NOT register anything. Review the command below, then run it"
Write-Host "yourself (as Administrator) only when you're ready to enable scheduled polling:"
Write-Host ""
Write-Host ("schtasks " + ($schtasksArgs -join ' '))
Write-Host ""
Write-Host "Notes on flags not expressible via schtasks command-line switches -"
Write-Host "set these in Task Scheduler's GUI (or via the ScheduledTasks PowerShell module)"
Write-Host "after creating the task, under the task's Properties:"
Write-Host "  - Settings > 'Stop the task if it runs longer than': 4 hours (a single poll"
Write-Host "    should finish in well under a minute; this is a generous safety ceiling)."
Write-Host "  - Settings > 'If the task fails, restart every': 1 minute, up to 3 attempts."
Write-Host "  - Settings > 'If the task is already running, then the following rule"
Write-Host "    applies': 'Do not start a new instance' (belt-and-suspenders - poll's"
Write-Host "    own single_instance_lock() already refuses to run a second overlapping"
Write-Host "    poll even if Task Scheduler's own overlap setting were misconfigured)."
Write-Host "  - General > 'Run whether user is logged on or not' (already implied by"
Write-Host "    /RU SYSTEM above, but confirm it in the GUI)."
