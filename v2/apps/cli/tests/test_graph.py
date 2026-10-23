"""`oe graph list` when OpenEngine cannot answer: an actionable message and a nonzero exit."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from openengine.cli.config.local import cache_directory
from openengine.supervisor.processes import nats
from openengine.supervisor.readiness.http import wait_until_healthy
from openengine.supervisor.state import ProcessState, Status, SupervisorState, state_path, write_state

ADVICE = "Start it with `oe up`; see `oe logs`."


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _graph_list(state_home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openengine.cli", "graph", "list"],
        env={**os.environ, "XDG_STATE_HOME": str(state_home)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _recorded(state_home: Path, nats_url: str) -> None:
    """A supervisor state naming `nats_url`, recorded under this live test process's pid."""
    write_state(
        state_path(state_home / "openengine"),
        SupervisorState(pid=os.getpid(), status=Status.READY, processes=[ProcessState("nats", 0, nats_url, Status.READY)]),
    )


def test_without_a_supervisor(tmp_path: Path) -> None:
    result = _graph_list(tmp_path)

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (no supervisor). {ADVICE}"
    assert result.stdout == ""


def test_when_nats_cannot_be_reached(tmp_path: Path) -> None:
    url = f"nats://127.0.0.1:{_free_port()}"
    _recorded(tmp_path, url)

    result = _graph_list(tmp_path)

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (cannot reach NATS at {url}). {ADVICE}"


@pytest.mark.integration
def test_when_nats_runs_but_ge_does_not(tmp_path: Path) -> None:
    port, monitor_port = _free_port(), _free_port()
    command = nats.server_command(
        nats.install_nats_server(cache_directory()), host="127.0.0.1", port=port,
        monitor_port=monitor_port, store=tmp_path / "nats",
    )
    server = nats.start(command, subprocess.DEVNULL)
    try:
        wait_until_healthy("nats-server", server, f"http://127.0.0.1:{monitor_port}/healthz", timeout=30)
        _recorded(tmp_path, f"nats://127.0.0.1:{port}")

        result = _graph_list(tmp_path)
    finally:
        server.terminate()
        server.wait()

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (no reply from ge). {ADVICE}"
