"""The supervisor's state file: how `oe` learns what the supervisor is doing.

The supervisor rewrites it as it starts, becomes ready or fails. The CLI reads
it and checks the recorded pid is still alive, since a supervisor that was
killed outright never gets to update it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    STARTING = "starting"
    READY = "ready"
    """Passed its readiness check."""
    RUNNING = "running"
    """Started; it has no readiness check yet."""
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessState:
    name: str
    pid: int
    address: str
    status: Status = Status.RUNNING


@dataclass(frozen=True)
class SupervisorState:
    pid: int
    status: Status
    processes: list[ProcessState] = field(default_factory=list)
    error: str | None = None


def state_path(state_directory: Path) -> Path:
    return state_directory / "supervisor.json"


def write_state(path: Path, state: SupervisorState) -> None:
    """Replace the file whole, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as partial:
        json.dump(asdict(state), partial)
    os.replace(partial.name, path)


def read_state(path: Path) -> SupervisorState | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return SupervisorState(
        pid=data["pid"],
        status=Status(data["status"]),
        processes=[_process(process) for process in data["processes"]],
        error=data.get("error"),
    )


def _process(data: dict[str, object]) -> ProcessState:
    """One process, from a file a supervisor of another version may have written.

    Fields this version does not know are ignored, and a missing status reads as
    running, so `oe status` and `oe down` still work across an upgrade.
    """
    known = {name: data[name] for name in _PROCESS_FIELDS if name in data}
    return ProcessState(**known | {"status": Status(known.get("status", Status.RUNNING))})


_PROCESS_FIELDS = tuple(field.name for field in fields(ProcessState))


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
