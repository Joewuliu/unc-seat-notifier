"""single_instance_lock() tests. Windows-only (msvcrt), matching this project's
Windows/Task Scheduler production target."""

from __future__ import annotations

import msvcrt
import os

import pytest

from unc_notifier.lock import LockUnavailable, single_instance_lock


def test_lock_can_be_acquired_and_released(tmp_path):
    lock_path = tmp_path / "unc_notifier.lock"

    with single_instance_lock(lock_path):
        pass

    # Released on exit - acquiring again immediately must succeed.
    with single_instance_lock(lock_path):
        pass


def test_lock_creates_its_parent_directory(tmp_path):
    lock_path = tmp_path / "nested" / "dir" / "unc_notifier.lock"

    with single_instance_lock(lock_path):
        assert lock_path.exists()


def test_overlapping_lock_acquisition_is_refused(tmp_path):
    lock_path = tmp_path / "unc_notifier.lock"

    with single_instance_lock(lock_path):
        with pytest.raises(LockUnavailable):
            with single_instance_lock(lock_path):
                pass  # pragma: no cover - never reached, second acquisition fails


def test_lock_is_released_even_if_the_wrapped_code_raises(tmp_path):
    lock_path = tmp_path / "unc_notifier.lock"

    with pytest.raises(ValueError, match="boom"):
        with single_instance_lock(lock_path):
            raise ValueError("boom")

    # A fresh acquisition must succeed - the failed run must not have left it held.
    with single_instance_lock(lock_path):
        pass


def test_a_lock_left_by_a_crashed_process_does_not_stay_stale(tmp_path):
    """Simulates a process that died without releasing the lock: acquires the OS
    lock directly and closes the file handle without unlocking first - exactly what
    happens when Windows tears down a killed or crashed process's open handles.
    The lock must not remain permanently stuck afterward."""
    lock_path = tmp_path / "unc_notifier.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    os.write(fd, b"0")
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    os.close(fd)  # simulated crash: no LK_UNLCK first

    with single_instance_lock(lock_path):
        pass
