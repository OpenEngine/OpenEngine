from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from openengine.supervisor import supervisor
from openengine.supervisor.processes import services
from openengine.supervisor.state import ProcessState, Status


def test_default_startup_always_runs_vcp_and_stops_it_first(monkeypatch, tmp_path: Path) -> None:
    started = []
    stopped = []
    broker = Mock(pid=100)

    def start(module, log, *, nats_url, environment):
        process = Mock(pid=101 + len(started))
        started.append((module, process, nats_url))
        if module == "virtual_control_plane":
            assert environment == {
                "OE_VCP_READY_FILE": str(tmp_path / "state" / "vcp.ready"),
                "OE_VCP_STATE_DIR": str(tmp_path / "state" / "vcp"),
            }
        return process

    monkeypatch.setattr(supervisor, "ShutdownRequest", Mock())
    monkeypatch.setattr(supervisor.nats, "ensure_ports_free", Mock())
    monkeypatch.setattr(supervisor.nats, "start", Mock(return_value=broker))
    monkeypatch.setattr(supervisor, "wait_for_ready_file", Mock())
    monkeypatch.setattr(supervisor, "wait_until_healthy", Mock())
    monkeypatch.setattr(supervisor.services, "start", start)
    readiness = Mock(
        side_effect=lambda name, process, *_: ProcessState(name, process.pid, "http://localhost/healthz", Status.READY)
    )
    monkeypatch.setattr(supervisor, "_wait_until_ready", readiness)
    monkeypatch.setattr(supervisor, "_watch", Mock())
    # Exercise the real reverse-order shutdown without spawning child processes.
    monkeypatch.setattr("openengine.supervisor.shutdown.processes.stop", stopped.append)
    settings = supervisor.Settings(
        state_directory=tmp_path / "state",
        cache_directory=tmp_path / "cache",
        host="127.0.0.1",
        nats_port=4222,
        nats_monitor_port=8222,
        nats_server=tmp_path / "nats-server",
    )

    assert supervisor.run(settings) == 0

    assert [module for module, _, _ in started] == [
        "connection_bus", "graph_engine", "tool_service", "virtual_control_plane",
    ]
    assert started[-1][2] == "nats://127.0.0.1:4222"
    assert stopped == [process for _, process, _ in reversed(started)] + [broker]
    assert [call.args[0] for call in readiness.call_args_list] == ["ge", "vcp"]


def test_vcp_has_a_ready_file_and_a_state_directory_variable() -> None:
    assert services.READY_FILE_VARIABLES["vcp"] == "OE_VCP_READY_FILE"
    assert services.STATE_DIR_VARIABLES["vcp"] == "OE_VCP_STATE_DIR"
