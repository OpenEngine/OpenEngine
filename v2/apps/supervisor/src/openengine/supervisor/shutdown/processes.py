"""Stop the supervisor's children."""

from __future__ import annotations

import subprocess

STOP_TIMEOUT_SECONDS = 10.0


def stop(process: subprocess.Popen, timeout: float = STOP_TIMEOUT_SECONDS) -> int:
    """Ask a process to exit cleanly, killing it if it does not."""
    if process.poll() is None:
        process.terminate()
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.wait()


def stop_all(processes: list[subprocess.Popen]) -> None:
    """Stop in reverse start order, so nothing outlives what it depends on."""
    for process in reversed(processes):
        stop(process)
