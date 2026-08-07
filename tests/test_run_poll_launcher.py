"""Tests for scripts/run_poll.ps1, the production Task Scheduler launcher. Runs the
real script via powershell.exe against synthetic repo skeletons under tmp_path -
never against this machine's real .env or real virtual environment - so these are
safe to run repeatedly and don't depend on this checkout's own venv being set up."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REAL_LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "run_poll.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="powershell.exe not found on PATH")


def _make_skeleton(tmp_path: Path, *, with_venv: bool = True, with_env: bool = True) -> Path:
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(REAL_LAUNCHER, scripts_dir / "run_poll.ps1")

    if with_venv:
        venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("")  # the launcher only checks existence, not contents
    if with_env:
        (repo_root / ".env").write_text("CONTACT_EMAIL=me@example.com\n")

    return repo_root


def _run_launcher(repo_root: Path, *, cwd: Path, extra_args: list[str] | None = None):
    args = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(repo_root / "scripts" / "run_poll.ps1"),
        *(extra_args or []),
    ]
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=30)


def test_dry_run_resolves_paths_from_script_location_not_the_callers_cwd(tmp_path):
    repo_root = _make_skeleton(tmp_path)
    unrelated_cwd = tmp_path  # deliberately not repo_root

    result = _run_launcher(repo_root, cwd=unrelated_cwd, extra_args=["-DryRun"])

    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout)
    assert Path(paths["RepoRoot"]) == repo_root.resolve()
    assert Path(paths["EnvFile"]) == (repo_root / ".env").resolve()
    assert Path(paths["LogDir"]) == (repo_root / "logs").resolve()
    assert Path(paths["VenvPython"]) == (repo_root / ".venv" / "Scripts" / "python.exe").resolve()


def test_dry_run_result_is_identical_regardless_of_working_directory(tmp_path):
    repo_root = _make_skeleton(tmp_path)

    from_repo_root = _run_launcher(repo_root, cwd=repo_root, extra_args=["-DryRun"])
    from_elsewhere = _run_launcher(repo_root, cwd=Path.home(), extra_args=["-DryRun"])

    assert from_repo_root.returncode == 0
    assert from_elsewhere.returncode == 0
    assert json.loads(from_repo_root.stdout) == json.loads(from_elsewhere.stdout)


def test_fails_clearly_when_venv_python_is_missing(tmp_path):
    repo_root = _make_skeleton(tmp_path, with_venv=False)

    result = _run_launcher(repo_root, cwd=tmp_path)

    assert result.returncode == 1
    assert "Virtual environment Python not found" in result.stderr


def test_fails_clearly_when_env_file_is_missing(tmp_path):
    repo_root = _make_skeleton(tmp_path, with_env=False)

    result = _run_launcher(repo_root, cwd=tmp_path)

    assert result.returncode == 1
    assert ".env not found" in result.stderr


def test_appends_a_dated_log_and_passes_through_the_child_process_exit_code(tmp_path):
    """Copies this same interpreter's own python.exe (plus its pyvenv.cfg, so it can
    still find its base install's stdlib from a new location) in as the stub - a real
    executable, so the launcher's actual invocation/redirection/exit-code plumbing
    runs for real. The skeleton has no unc_notifier install alongside it, so
    `-m unc_notifier` deterministically fails with "No module named unc_notifier"
    before touching any config, network, or email - exactly the kind of predictable
    nonzero exit this test needs to prove the launcher creates logs\\, appends
    start/end markers, and returns the exact exit code the child produced."""
    repo_root = _make_skeleton(tmp_path)
    real_venv_dir = Path(sys.executable).resolve().parent.parent
    shutil.copy(real_venv_dir / "Scripts" / "python.exe", repo_root / ".venv" / "Scripts")
    shutil.copy(real_venv_dir / "pyvenv.cfg", repo_root / ".venv")

    result = _run_launcher(repo_root, cwd=tmp_path)

    log_files = list((repo_root / "logs").glob("poll_*.log"))
    assert len(log_files) == 1
    log_contents = log_files[0].read_text()
    assert "===== poll run started" in log_contents
    assert f"exit={result.returncode} =====" in log_contents
    assert result.returncode != 0  # config is deliberately incomplete
