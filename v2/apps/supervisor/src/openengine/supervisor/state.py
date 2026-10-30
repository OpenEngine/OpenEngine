"""The supervisor's state file: how `oe` learns what the supervisor is doing.

The supervisor rewrites it as it starts, becomes ready or fails. The CLI reads
it and checks the recorded pid is still alive, since a supervisor that was
killed outright never gets to update it.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path

from openengine.messaging.broker import open_private_directory, reject_extended_acl


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
    directory = open_private_directory(path.parent, create=True)
    os.close(directory)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as partial:
        reject_extended_acl(partial.fileno())
        json.dump(asdict(state), partial)
    os.replace(partial.name, path)


class UnsafeState(PermissionError):
    def __init__(self) -> None:
        super().__init__("supervisor state must be a private regular file owned by this user")


def _state_bytes(path: Path) -> bytes:
    directory = open_private_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            reject_extended_acl(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise UnsafeState
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read(65537)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def read_state(path: Path) -> SupervisorState | None:
    try:
        raw = _state_bytes(path)
    except FileNotFoundError:
        return None
    if len(raw) > 65536:
        return None
    try:
        data = json.loads(raw)
        return SupervisorState(
            pid=data["pid"],
            status=Status(data["status"]),
            processes=[_process(process) for process in data["processes"]],
            error=data.get("error"),
        )
    except (ValueError, TypeError, KeyError):
        return None


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
