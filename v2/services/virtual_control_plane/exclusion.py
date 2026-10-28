"""Exclusive ownership of the VCP state directory.

A worktree/host-process workspace (#1054) lives directly under this
directory, so two VCP processes sharing it would both believe they own the
same checkouts. A `flock` on a lock file inside it gives one process
exclusive ownership for as long as it runs, released automatically if it
dies; a second process that cannot take the lock fails its own startup
rather than racing the first one.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

LOCK_FILE = ".vcp.lock"


class AlreadyRunning(RuntimeError):
    def __init__(self, directory: Path) -> None:
        super().__init__(f"another process already owns {directory}")
        self.directory = directory


class StateDirectory:
    """Holds the exclusive lock on `directory` from `acquire()` until `release()`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._fd: int | None = None

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / LOCK_FILE, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AlreadyRunning(self.directory) from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "StateDirectory":
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.release()
