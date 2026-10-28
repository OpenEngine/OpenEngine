"""Wait for a started process to write its ready file, and to report itself healthy over HTTP."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

POLL_SECONDS = 0.1


class NotReady(RuntimeError):
    pass


class ExitedBeforeReady(NotReady):
    def __init__(self, name: str, code: int) -> None:
        super().__init__(f"{name} exited with status {code} before it was ready")


class ReadyTimeout(NotReady):
    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(f"{name} was not ready within {timeout:.0f}s")


class Cancelled(NotReady):
    def __init__(self, name: str) -> None:
        super().__init__(f"shutdown requested while waiting for {name}")


def wait_until_healthy(
    name: str,
    process: subprocess.Popen,
    url: str,
    *,
    timeout: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Return once `url` answers 200; raise if the process exits, time runs out, or shutdown starts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            raise Cancelled(name)
        if (code := process.poll()) is not None:
            raise ExitedBeforeReady(name, code)
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(POLL_SECONDS)
    raise ReadyTimeout(name, timeout)


def wait_for_ready_file(
    name: str,
    process: subprocess.Popen,
    path: Path,
    *,
    timeout: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    """The text a process writes to `path` once it serves, such as its health URL; raise as `wait_until_healthy` does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            raise Cancelled(name)
        if (code := process.poll()) is not None:
            raise ExitedBeforeReady(name, code)
        if path.is_file() and (url := path.read_text().strip()):
            return url
        time.sleep(POLL_SECONDS)
    raise ReadyTimeout(name, timeout)
