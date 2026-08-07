"""Tests for scripts/register_task.ps1, remove_task.ps1, and verify_task.ps1 - the
Task Scheduler registration workflow. Runs the real scripts via powershell.exe
against synthetic repo skeletons under tmp_path (never this checkout's real
scripts\\run_poll.ps1 or .env), and always with a random -TaskName so nothing here
can collide with, or depend on, whatever "UNC Seat Notifier" task may or may not
already be registered on this machine.

Registering/unregistering a real scheduled task requires an elevated ("Run as
Administrator") PowerShell session - confirmed unavailable in this environment
(Register-ScheduledTask fails with "Access is denied" for a normal session). So
these tests deliberately only exercise the PREVIEW paths (which never call
Register-ScheduledTask/Unregister-ScheduledTask) plus the read-only "task doesn't
exist" paths - never a real registration or removal.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="powershell.exe not found on PATH")


def _random_task_name() -> str:
    return f"UNC Seat Notifier PyTest {uuid.uuid4().hex[:8]}"


def _make_skeleton(tmp_path: Path, *, with_launcher: bool = True) -> Path:
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in ("register_task.ps1", "remove_task.ps1", "verify_task.ps1"):
        shutil.copy(SCRIPTS_DIR / name, scripts_dir / name)
    if with_launcher:
        (scripts_dir / "run_poll.ps1").write_text("# stub launcher for tests\nexit 0\n")
    (repo_root / ".env").write_text(
        "CONTACT_EMAIL=me@example.com\nSMTP_PASSWORD=topsecretpass\n"
    )
    return repo_root


def _run(script: str, repo_root: Path, *, args: list[str] | None = None):
    return subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(repo_root / "scripts" / script),
            *(args or []),
        ],
        capture_output=True, text=True, timeout=30,
    )


# --- register_task.ps1 --------------------------------------------------------------


def test_preview_registers_nothing_and_exits_zero(tmp_path):
    repo_root = _make_skeleton(tmp_path)
    task_name = _random_task_name()

    result = _run("register_task.ps1", repo_root, args=["-TaskName", task_name])

    assert result.returncode == 0, result.stderr
    assert "PREVIEW ONLY" in result.stdout
    assert "Registered '" not in result.stdout


def test_preview_leaves_no_task_registered(tmp_path):
    repo_root = _make_skeleton(tmp_path)
    task_name = _random_task_name()

    _run("register_task.ps1", repo_root, args=["-TaskName", task_name])
    lookup = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", f"Get-ScheduledTask -TaskName '{task_name}'"],
        capture_output=True, text=True, timeout=30,
    )

    # Get-ScheduledTask errors out (non-zero / error text) when the task doesn't exist.
    assert lookup.returncode != 0 or "No MSFT_ScheduledTask objects found" in lookup.stderr


def test_generated_action_points_to_the_exact_launcher_with_correct_start_in(tmp_path):
    repo_root = _make_skeleton(tmp_path)
    task_name = _random_task_name()

    result = _run("register_task.ps1", repo_root, args=["-TaskName", task_name])

    expected_launcher = str(repo_root / "scripts" / "run_poll.ps1")
    assert expected_launcher in result.stdout
    assert "powershell.exe" in result.stdout.lower()
    start_in_line = next(line for line in result.stdout.splitlines() if line.startswith("Start in"))
    assert start_in_line.split(":", 1)[1].strip() == str(repo_root)


def test_cadence_is_five_minutes(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    trigger_line = next(line for line in result.stdout.splitlines() if line.startswith("Trigger"))
    assert "PT5M" in trigger_line


def test_overlap_behavior_is_ignore_new(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    overlap_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Overlap behavior")
    )
    assert "MultipleInstances=IgnoreNew" in overlap_line


def test_maximum_runtime_and_restart_policy_match_requirements(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    runtime_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Maximum runtime")
    )
    restart_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Restart on failure")
    )
    assert "PT4M" in runtime_line  # 4 minutes
    assert "RestartCount=3" in restart_line
    assert "RestartInterval=PT1M" in restart_line  # 1 minute apart


def test_preview_never_prints_secrets_from_env(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    assert "topsecretpass" not in result.stdout
    assert "SMTP_PASSWORD" not in result.stdout


def test_fails_clearly_when_launcher_is_missing(tmp_path):
    repo_root = _make_skeleton(tmp_path, with_launcher=False)

    result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    assert result.returncode == 1
    assert "Launcher not found" in result.stderr


# --- repetition duration regression --------------------------------------------------
#
# Real production registration once failed with:
#   Register-ScheduledTask: The task XML contains a value which is incorrectly
#   formatted or out of range. (8,42):Duration:P99999999DT23H59M59S
#   HRESULT 0x80041318
# caused by -RepetitionDuration ([TimeSpan]::MaxValue). Fixed by omitting
# -RepetitionDuration entirely (Task Scheduler's documented way to repeat
# indefinitely), with an opt-in -RepetitionYears fallback that uses a finite,
# ordinary-sized Duration instead. These tests guard against ever reintroducing
# the oversized value, and independently verify (via the raw Task Scheduler COM
# API, not the ScheduledTasks module register_task.ps1 itself uses to register)
# that the chosen "omitted Duration" shape matches Task Scheduler's own
# schema-correct representation for indefinite repetition.


def test_preview_never_contains_the_oversized_duration_string(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    default_result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])
    finite_result = _run(
        "register_task.ps1", repo_root,
        args=["-TaskName", _random_task_name(), "-RepetitionYears", "10"],
    )

    for result in (default_result, finite_result):
        assert result.returncode == 0, result.stderr
        assert "P99999999" not in result.stdout
        assert "MaxValue" not in result.stdout


def test_default_trigger_omits_duration_for_indefinite_repetition(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("register_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    trigger_line = next(line for line in result.stdout.splitlines() if line.startswith("Trigger"))
    assert "no repetition end" in trigger_line
    assert "Duration=" not in trigger_line


def test_repetition_years_fallback_uses_a_finite_ordinary_sized_duration(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run(
        "register_task.ps1", repo_root,
        args=["-TaskName", _random_task_name(), "-RepetitionYears", "10"],
    )

    trigger_line = next(line for line in result.stdout.splitlines() if line.startswith("Trigger"))
    assert "Duration=P3650D" in trigger_line
    assert "P99999999" not in trigger_line


def test_repetition_years_rejects_absurdly_large_values(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run(
        "register_task.ps1", repo_root,
        args=["-TaskName", _random_task_name(), "-RepetitionYears", "999999"],
    )

    assert result.returncode != 0
    assert "P99999999" not in result.stdout


def _build_time_trigger_xml(*, duration: str | None) -> str:
    """Builds a real Task Scheduler TimeTrigger directly against the raw Task
    Scheduler COM API (Schedule.Service.NewTask()) and returns its XmlText - purely
    local task-definition construction, no elevation needed and no call to the
    Task Scheduler service, so this never registers anything. An independent
    cross-check of what real Task Scheduler XML a given Duration configuration
    produces, separate from the ScheduledTasks-module code path register_task.ps1
    itself uses."""
    duration_line = f'$trigger.Repetition.Duration = "{duration}"' if duration else ""
    script = f"""
$service = New-Object -ComObject "Schedule.Service"
$service.Connect()
$task = $service.NewTask(0)
$trigger = $task.Triggers.Create(1)
$trigger.StartBoundary = (Get-Date).ToString("s")
$trigger.Repetition.Interval = "PT5M"
{duration_line}
Write-Output $task.XmlText
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_omitted_duration_matches_task_schedulers_schema_for_indefinite_repetition():
    """The exact configuration register_task.ps1 uses by default (Interval set,
    Duration never assigned) produces the schema-correct "repeat indefinitely"
    shape: an <Interval> element but no <Duration> element under <Repetition> at
    all. This can only confirm the generated XML's *shape* matches Task
    Scheduler's own documented indefinite-repetition representation, offline and
    without elevation - it cannot, by itself, prove the live service will accept
    a real registration (that requires an elevated session; see PROJECT_STATUS.md)."""
    xml = _build_time_trigger_xml(duration=None)
    repetition_block = re.search(r"<Repetition>.*?</Repetition>", xml, re.DOTALL).group()

    assert "<Interval>PT5M</Interval>" in repetition_block
    assert "<Duration>" not in repetition_block
    assert "P99999999" not in xml


def test_the_original_oversized_duration_is_detectable_in_generated_xml():
    """Proves this regression check would have caught the original bug: explicitly
    reproducing the old -RepetitionDuration ([TimeSpan]::MaxValue) value shows up
    verbatim in the generated XML - the exact string the live Task Scheduler
    service rejected with "out of range" (HRESULT 0x80041318) against a real
    Windows installation."""
    xml = _build_time_trigger_xml(duration="P99999999DT23H59M59S")

    assert "<Duration>P99999999DT23H59M59S</Duration>" in xml


# --- remove_task.ps1 -----------------------------------------------------------------


def test_remove_reports_nothing_to_remove_for_a_nonexistent_task(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("remove_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    assert result.returncode == 0
    assert "nothing to remove" in result.stdout.lower()


def test_remove_confirmation_guard_exists_before_unregister_in_source():
    """Belt-and-suspenders regression check on the script's source: the
    -ConfirmRemove gate must appear before the Unregister-ScheduledTask call, so a
    future edit can't accidentally reorder them and remove the safety gate. The
    happy-path "task exists, gets removed" flow can't be exercised end-to-end in
    tests - registering a real task requires an elevated session, unavailable here."""
    source = (SCRIPTS_DIR / "remove_task.ps1").read_text()

    guard_index = source.index("if (-not $ConfirmRemove)")
    # rindex: the doc comment block mentions "Unregister-ScheduledTask" by name too -
    # the real call is the last occurrence in the file.
    unregister_index = source.rindex("Unregister-ScheduledTask")

    assert guard_index < unregister_index


# --- verify_task.ps1 -------------------------------------------------------------------


def test_verify_reports_missing_task_and_exits_nonzero(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    result = _run("verify_task.ps1", repo_root, args=["-TaskName", _random_task_name()])

    assert result.returncode == 1
    assert "No scheduled task named" in result.stdout
