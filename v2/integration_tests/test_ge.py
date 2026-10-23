"""GE started by the supervisor (`oe up`), queried over real NATS, drained on stop, and refused when it cannot be ready."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

import nats
import pytest
from openengine.contracts.validate import load_contract, validate_message

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def oe(tmp_path: Path):
    environment = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "OE_LOG_FORMAT": "json"}

    def run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "openengine.cli", *arguments],
            env={**environment, **(env or {})},
            capture_output=True,
            text=True,
            timeout=240,
        )

    yield run
    run("down")


def _up(oe, port: int, **env: str) -> subprocess.CompletedProcess[str]:
    return oe("up", "--nats-port", str(port), "--nats-monitor-port", str(_free_port()), env=env)


async def _request(url: str, subject: str, body: dict) -> dict:
    connection = await nats.connect(url)
    try:
        return json.loads((await connection.request(subject, json.dumps(body).encode(), timeout=5)).data)
    finally:
        await connection.close()


def _ge_lines(oe) -> list[dict]:
    lines = [json.loads(line) for line in oe("logs", "--json").stdout.splitlines() if line.startswith("{")]
    return [line for line in lines if line["service.name"] == "openengine-ge"]


def test_ge_serves_graphs_list_once_ready_and_drains_on_stop(oe) -> None:
    port = _free_port()
    up = _up(oe, port)
    assert up.returncode == 0, up.stderr

    state = json.loads(oe("status", "--json").stdout)
    ge = next(process for process in state["processes"] if process["name"] == "ge")
    assert ge["status"] == "ready"
    assert ge["address"].startswith("http://127.0.0.1:") and ge["address"].endswith("/healthz")
    with urlopen(ge["address"], timeout=2) as health:
        assert health.status == 200

    subject = "oe.v1.ge.query.graphs.list"
    reply = asyncio.run(_request(f"nats://127.0.0.1:{port}", subject, {"request_id": "req_it", "params": {}}))
    assert reply == {"request_id": "req_it", "status": "ok", "result": {"items": [], "next_cursor": None}}
    assert validate_message(load_contract("graph_engine"), "ListGraphsResponse", reply, in_reply_to=subject) == []

    bad = asyncio.run(_request(f"nats://127.0.0.1:{port}", subject, {"params": {"unknown": 1}}))
    assert bad["error"]["code"] == "INVALID_ARGUMENT"

    assert oe("down").returncode == 0
    bodies = [line["body"] for line in _ge_lines(oe)]
    served = bodies[bodies.index("nats connected"):]
    assert served[:4] == ["nats connected", "serving oe.v1.ge.query.>", "serving oe.v1.ge.control.>", "ready"]
    # The unknown param passes the envelope check, so it is logged as a request before graphs.list's schema rejects it.
    assert served[4:6] == ["graphs.list request", "graphs.list request"]
    assert served[6].startswith("graphs.list rejected: INVALID_ARGUMENT")
    assert served[7:] == ["draining", "stopped"]


def test_supervisor_fails_when_ge_cannot_become_ready(oe, tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        # GE cannot bind its health port, so it never reports ready.
        up = _up(oe, _free_port(), OE_GE_HEALTH_PORT=str(taken.getsockname()[1]))

    assert up.returncode == 1
    assert "ge exited with status 1 before it was ready" in up.stderr
    assert oe("status").returncode == 1
    # The supervisor has exited; its last state stays behind for `oe up` to report.
    state = json.loads((tmp_path / "state" / "openengine" / "supervisor.json").read_text())
    assert state["status"] == "failed"
    assert state["error"] == "ge exited with status 1 before it was ready"
    assert {process["name"]: process["status"] for process in state["processes"]}["ge"] == "failed"


def test_ge_stops_on_sigterm_while_nats_is_unreachable(tmp_path: Path) -> None:
    ge = subprocess.Popen(
        [sys.executable, "-m", "graph_engine"],
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"),
             "OE_NATS_URL": f"nats://127.0.0.1:{_free_port()}", "OE_GE_READY_FILE": str(tmp_path / "ready")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            ge.wait(timeout=2)  # still retrying its first connection
        ge.terminate()
        assert ge.wait(timeout=5) == 0
    finally:
        ge.kill()
    assert not (tmp_path / "ready").exists()
