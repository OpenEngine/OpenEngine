"""Stop the supervisor's children."""

from __future__ import annotations

import subprocess
import time

STOP_TIMEOUT_SECONDS = 10.0


def stop(process: subprocess.Popen, timeout: float = STOP_TIMEOUT_SECONDS) -> int:
    """Ask a process to exit cleanly, killing it if it does not."""
    if process.poll() is None:
        process.terminate()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.wait(timeout=2.0)


def stop_all(processes: list[subprocess.Popen]) -> None:
    """Stop in reverse start order, so nothing outlives what it depends on."""
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    unreaped = None
    for process in reversed(processes):
        try:
            stop(process, timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            # Still stop the remaining children if one cannot be reaped.
            unreaped = error
    if unreaped is not None:
        raise unreaped
