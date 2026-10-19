from __future__ import annotations

import os
from pathlib import Path

from openengine.supervisor.state import (
    ProcessState,
    Status,
    SupervisorState,
    is_alive,
    read_state,
    state_path,
    write_state,
)


def test_round_trips_the_state_file(tmp_path: Path) -> None:
    path = state_path(tmp_path / "state")
    state = SupervisorState(pid=7, status=Status.READY, processes=[ProcessState("nats", 8, "nats://127.0.0.1:4222")])

    write_state(path, state)

    assert read_state(path) == state


def test_reads_a_missing_or_unreadable_file_as_no_state(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    assert read_state(path) is None
    path.write_text("{")
    assert read_state(path) is None


def test_knows_whether_a_pid_is_alive() -> None:
    assert is_alive(os.getpid())
    assert not is_alive(2**22 + 1)
