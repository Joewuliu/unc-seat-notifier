"""Single-instance cross-process lock for `poll`, so an overlapping scheduled run
refuses to start while a previous one is still in progress.

Implemented with the stdlib ``msvcrt`` Windows file-locking API rather than a
hand-rolled PID file: the lock is tied to the OS file handle, which the OS releases
automatically the moment the holding process exits or is killed - including an
ungraceful crash. That means a "stale lock" that permanently blocks future polls
simply cannot happen here; there is no separate staleness check to get wrong.
"""

from __future__ import annotations

import msvcrt
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOCK_REGION_SIZE = 1


class LockUnavailable(Exception):
    """Raised when another process already holds the lock."""


@contextmanager
def single_instance_lock(lock_path: str | Path) -> Iterator[None]:
    """Acquire an exclusive, non-blocking lock on ``lock_path`` for the duration of
    the ``with`` block. Raises LockUnavailable immediately (never blocks) if another
    process already holds it. Always releases on the way out, including when the
    wrapped code raises."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    try:
        if os.fstat(fd).st_size < _LOCK_REGION_SIZE:
            os.write(fd, b"0" * _LOCK_REGION_SIZE)
        os.lseek(fd, 0, os.SEEK_SET)

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_REGION_SIZE)
        except OSError as exc:
            raise LockUnavailable(
                f"another instance already holds the lock at {lock_path}"
            ) from exc

        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_REGION_SIZE)
    finally:
        os.close(fd)
