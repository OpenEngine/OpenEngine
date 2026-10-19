"""`oe up`, `status`, `logs` and `down` against a real supervisor and nats-server.

The real cache directory is kept, so only the first run downloads nats-server.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
    except OSError:
        return False
    return True


@pytest.fixture
def oe(tmp_path: Path):
    environment = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "openengine.cli", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=240,
        )

    yield run
    run("down")


def test_up_runs_nats_in_the_background_until_down(oe, tmp_path: Path) -> None:
    port, monitor_port = _free_port(), _free_port()

    up = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(monitor_port))

    assert up.returncode == 0, up.stderr
    assert f"nats://127.0.0.1:{port}" in up.stdout
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        assert client.recv(4096).startswith(b"INFO ")

    status = oe("status", "--json")
    assert status.returncode == 0
    state = json.loads(status.stdout)
    assert state["status"] == "ready"
    assert [process["name"] for process in state["processes"]] == ["nats"]

    again = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(monitor_port))
    assert again.returncode == 0
    assert "already running" in again.stdout

    logs = oe("logs")
    assert "Server is ready" in logs.stdout
    assert str(tmp_path / "state" / "openengine" / "nats") in logs.stdout

    down = oe("down")
    assert down.returncode == 0
    assert "stopped" in down.stdout
    deadline = time.monotonic() + 5
    while _listening(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _listening(port)
    assert oe("status").returncode == 1


def test_up_reports_why_nats_could_not_start(oe) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        up = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(_free_port()))

    assert up.returncode == 1
    assert "nats-server exited with status" in up.stderr
    assert oe("status").returncode == 1
