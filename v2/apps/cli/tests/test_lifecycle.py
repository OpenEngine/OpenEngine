"""`oe up`, `status`, `logs`, `migrate` and `down` against a real supervisor and nats-server.

The real cache directory is kept, so only the first run downloads nats-server.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openengine.cli.migrations.streams import streams_directory
from openengine.messaging.broker import load_trust

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


def test_up_runs_nats_in_the_background_until_down(oe, tmp_path: Path) -> None:
    port, monitor_port = _free_port(), _free_port()

    up = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(monitor_port))

    assert up.returncode == 0, up.stderr
    assert f"tls://127.0.0.1:{port}" in up.stdout
    trust = load_trust(tmp_path / "state" / "openengine" / "broker.json")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=trust.ca_pem)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=trust.server_hostname) as client:
            assert client.recv(4096).startswith(b"INFO ")

    status = oe("status", "--json")
    assert status.returncode == 0
    state = json.loads(status.stdout)
    assert state["status"] == "ready"
    assert [process["name"] for process in state["processes"]] == ["nats", "cxn", "ge", "tools", "vcp"]

    again = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(monitor_port))
    assert again.returncode == 0
    assert "already running" in again.stdout

    logs = oe("logs")
    assert "Server is ready" in logs.stdout
    assert str(tmp_path / "state" / "openengine" / "nats") in logs.stdout
    assert f"[INF] supervisor: ready, NATS on tls://127.0.0.1:{port}" in logs.stdout
    raw = oe("logs", "--json")
    ready = next(json.loads(line) for line in raw.stdout.splitlines() if '"ready, NATS on' in line)
    assert ready["service.name"] == "openengine-supervisor"
    assert ready["severity_text"] == "INFO"
    # The services boot after the supervisor reports ready, so wait for their last boot line.
    deadline = time.monotonic() + 15
    while raw.stdout.count('"body": "ready"') < 4 and time.monotonic() < deadline:
        time.sleep(0.2)
        raw = oe("logs", "--json")
    for service in ("cxn", "tools"):
        boot = [json.loads(line) for line in raw.stdout.splitlines() if f'"service.name": "openengine-{service}"' in line]
        assert [line["body"] for line in boot][:4] == ["starting", "database configured", "nats configured", "ready"]
        assert len({line["trace_id"] for line in boot[:4]}) == 1
        assert boot[2]["attributes"]["server.address"] == f"tls://127.0.0.1:{port}"
    # ge connects and serves before it is ready; integration_tests/test_ge.py covers it.
    ge = [json.loads(line) for line in raw.stdout.splitlines() if '"service.name": "openengine-ge"' in line]
    connected = next(line for line in ge if line["body"] == "nats connected")
    assert connected["attributes"]["server.address"] == f"tls://127.0.0.1:{port}"

    down = oe("down")
    assert down.returncode == 0
    assert "stopped" in down.stdout
    deadline = time.monotonic() + 5
    while _listening(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _listening(port)
    assert oe("status").returncode == 1


def test_up_starts_the_virtual_control_plane_by_default(oe, tmp_path: Path) -> None:
    port, monitor_port = _free_port(), _free_port()

    up = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(monitor_port))
    assert up.returncode == 0, up.stderr

    status = oe("status", "--json")
    state = json.loads(status.stdout)
    assert state["status"] == "ready"
    assert [process["name"] for process in state["processes"]] == ["nats", "cxn", "ge", "tools", "vcp"]
    vcp = next(process for process in state["processes"] if process["name"] == "vcp")
    assert vcp["status"] == "ready"
    assert vcp["address"].startswith("http://127.0.0.1:")

    raw = oe("logs", "--json")
    boot = [json.loads(line) for line in raw.stdout.splitlines() if '"service.name": "openengine-vcp"' in line]
    bodies = [line["body"] for line in boot]
    assert bodies[:3] == ["starting", "state directory owned", "database configured"]
    assert "nats connected" in bodies
    assert bodies[-1] == "ready"
    assert (tmp_path / "state" / "openengine" / "vcp").is_dir()

    down = oe("down")
    assert down.returncode == 0
    assert "stopped" in down.stdout
    # Stopping vcp is just stopping its process; it must not touch the workspaces it owns.
    assert (tmp_path / "state" / "openengine" / "vcp").is_dir()


def test_up_reports_why_nats_could_not_start(oe) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]

        up = oe("up", "--nats-port", str(port), "--nats-monitor-port", str(_free_port()))

    assert up.returncode == 1
    assert "nats-server exited with status" in up.stderr
    assert oe("status").returncode == 1


def test_migrate_creates_the_databases_and_streams_once_and_updates_drifted_ones(oe, tmp_path: Path) -> None:
    port = _free_port()
    assert oe("up", "--nats-port", str(port), "--nats-monitor-port", str(_free_port())).returncode == 0

    first = oe("migrate")
    assert first.returncode == 0, first.stderr
    databases, streams = first.stdout.split("\n\n")
    # GE brings its own database to head as it starts, so only the others are new.
    assert databases.count("created") == 3
    assert next(line for line in databases.splitlines() if line.startswith("ge ")).endswith("unchanged")
    assert streams.count("created") == 6
    assert "cxn-ingress" in streams
    for service in ("cxn", "ge", "tools", "vcp"):
        assert (tmp_path / "state" / "openengine" / "databases" / f"{service}.sqlite3").exists()

    again = oe("migrate")
    assert again.returncode == 0, again.stderr
    assert again.stdout.count("unchanged") == 10

    streams = tmp_path / "streams"
    streams.mkdir()
    config = json.loads((streams_directory() / "ge-events.json").read_text())
    (streams / "ge-events.json").write_text(json.dumps({**config, "description": "changed"}))
    changed = oe("migrate", "--streams", str(streams))
    assert changed.returncode == 0, changed.stderr
    assert "ge-events  updated" in changed.stdout


def test_migrate_reports_a_backend_it_cannot_use(oe) -> None:
    assert oe("up", "--nats-port", str(_free_port()), "--nats-monitor-port", str(_free_port())).returncode == 0

    migrate = oe("migrate", env={"OE_DATABASE_BACKEND": "postgres"})

    assert migrate.returncode == 1
    assert "postgres backend does not implement" in migrate.stderr
    assert "Traceback" not in migrate.stderr


def test_migrate_needs_a_running_openengine(oe) -> None:
    migrate = oe("migrate")

    assert migrate.returncode == 1
    assert "not running" in migrate.stderr
