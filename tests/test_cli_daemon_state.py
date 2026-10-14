"""Daemon health as an explicit state machine: `DaemonState`, how a probe maps to it, and what callers do with it."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import socket
import threading
from collections.abc import Iterator
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from engine.apps.cli import connect, daemon
from engine.cli.backends import Backend

STATE_NAMES = {"down", "foreign", "incompatible", "ready", "starting"}


def _openengine(**overrides) -> bytes:
    return json.dumps({"service": "openengine", "version": "9.9.9", "api_version": daemon.API_VERSION, "ready": True, **overrides}).encode()


def test_daemon_state_is_a_str_enum_of_the_five_states():
    assert issubclass(daemon.DaemonState, StrEnum)
    assert {state.value for state in daemon.DaemonState} == STATE_NAMES
    # Values stay the strings `engine daemon status --json` already prints.
    assert daemon.DaemonState.READY == "ready"
    assert daemon.DaemonState.STARTING == "starting"
    assert daemon.DaemonState.INCOMPATIBLE == "incompatible"
    assert daemon.DaemonState.FOREIGN == "foreign"
    assert daemon.DaemonState.DOWN == "down"


# `derive_state(status, body)` is pure: `status` is the HTTP status code, or None when
# the URL could not be reached, and `body` is the response as `_parse_health` reads it.
PROBES = [
    pytest.param(200, _openengine(), "READY", id="ready"),
    pytest.param(200, _openengine(ready=False), "STARTING", id="not-ready-flag"),
    pytest.param(200, _openengine(ready=None), "STARTING", id="missing-ready-flag"),
    pytest.param(503, _openengine(ready=False), "STARTING", id="starting-http-error-body"),
    pytest.param(503, b'{"service": "nginx"}', "FOREIGN", id="503-from-another-service"),
    pytest.param(503, b"<html>Service Unavailable</html>", "FOREIGN", id="503-not-json"),
    pytest.param(500, _openengine(ready=False), "FOREIGN", id="http-500"),
    pytest.param(404, b"Not Found", "FOREIGN", id="http-404"),
    pytest.param(200, b"<html>hello</html>", "FOREIGN", id="non-json"),
    pytest.param(200, b'["openengine"]', "FOREIGN", id="json-not-an-object"),
    pytest.param(200, b'{"service": "grafana", "ready": true}', "FOREIGN", id="non-openengine"),
    pytest.param(200, _openengine(api_version=daemon.API_VERSION + 1), "INCOMPATIBLE", id="unsupported-api-version"),
    pytest.param(200, _openengine(api_version=None), "INCOMPATIBLE", id="missing-api-version"),
    pytest.param(None, None, "DOWN", id="unreachable"),
]


@pytest.mark.parametrize("status, raw, expected", PROBES)
def test_derive_state_maps_every_probe_outcome(status, raw, expected):
    state = daemon.derive_state(status, daemon._parse_health(raw))

    assert state is daemon.DaemonState[expected]


@pytest.fixture
def health_server() -> Iterator[tuple[str, dict]]:
    """A local `/api/health` endpoint answering with whatever `reply` holds."""
    reply: dict = {"status": 200, "body": _openengine()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(reply["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply["body"])

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", reply
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status, raw, expected", [probe for probe in PROBES if probe.values[0] is not None])
def test_check_health_returns_the_state_for_a_live_endpoint(health_server, status, raw, expected):
    url, reply = health_server
    reply.update(status=status, body=raw)

    state, _body, detail = daemon.check_health(url)

    assert state is daemon.DaemonState[expected]
    assert detail


def test_check_health_reports_an_unreachable_url_as_down():
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    state, body, detail = daemon.check_health(url, timeout=0.5)

    assert state is daemon.DaemonState.DOWN
    assert body is None
    assert detail.startswith(f"cannot reach {url}")


@pytest.mark.parametrize("ready, expected", [(True, "READY"), (False, "STARTING")])
def test_health_treats_another_api_version_as_ours_to_manage(health_server, ready, expected):
    url, reply = health_server
    reply["body"] = _openengine(api_version=daemon.API_VERSION + 1, ready=ready)

    state, _body = daemon.health(url)

    assert state is daemon.DaemonState[expected]


def test_wait_for_stops_at_a_wanted_state(monkeypatch):
    answers = iter([daemon.DaemonState.STARTING, daemon.DaemonState.STARTING, daemon.DaemonState.READY])
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (next(answers), {}))
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)

    state, _body = daemon.wait_for("http://engine.test", {daemon.DaemonState.READY, daemon.DaemonState.FOREIGN}, 5.0)

    assert state is daemon.DaemonState.READY


def test_every_state_has_exactly_one_exit_code():
    assert set(daemon.STATE_EXIT_CODES) == set(daemon.DaemonState)
    assert daemon.STATE_EXIT_CODES[daemon.DaemonState.READY] == daemon.EXIT_OK
    for state in set(daemon.DaemonState) - {daemon.DaemonState.READY}:
        assert daemon.STATE_EXIT_CODES[state] == daemon.EXIT_FAILED


@pytest.mark.parametrize("state", sorted(STATE_NAMES))
def test_status_exit_code_comes_from_the_mapping(monkeypatch, capsys, tmp_path, state):
    class Stopped:
        name = "process"

        def running(self) -> bool:
            return False

    spec = daemon.ServiceSpec("/venv/bin/engine-web", str(tmp_path / "engine.toml"), 4420, str(tmp_path / "engine-web.log"), {})
    monkeypatch.setattr(daemon, "current", lambda: (Stopped(), spec))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (daemon.DaemonState(state), None))
    monkeypatch.setattr(daemon, "STATE_EXIT_CODES", {member: 40 + index for index, member in enumerate(daemon.DaemonState)})

    result = daemon.command_status(argparse.Namespace(json=True))

    assert result == daemon.STATE_EXIT_CODES[daemon.DaemonState(state)]
    assert json.loads(capsys.readouterr().out)["health"] == state


def test_ensure_ready_starts_a_down_local_daemon(monkeypatch):
    answers = iter([
        (daemon.DaemonState.DOWN, None, "cannot reach http://127.0.0.1:4364"),
        (daemon.DaemonState.READY, {}, "OpenEngine 9.9.9 is ready"),
    ])
    started = []
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: next(answers))
    monkeypatch.setattr(connect.daemon, "read_record", lambda: object())
    monkeypatch.setattr(connect.daemon, "start_service", lambda: started.append(True))

    connect.ensure_ready(Backend("local", "http://127.0.0.1:4364"))

    assert started == [True]


@pytest.mark.parametrize("state", ["STARTING", "INCOMPATIBLE", "FOREIGN"])
def test_ensure_ready_refuses_any_state_but_ready(monkeypatch, state):
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: (daemon.DaemonState[state], None, f"detail for {state}"))
    monkeypatch.setattr(connect.daemon, "start_service", lambda: pytest.fail("only a down daemon is started"))

    with pytest.raises(RuntimeError, match=f"detail for {state}"):
        connect.ensure_ready(Backend("local", "http://127.0.0.1:4364"))


def _state_literals(module) -> list[str]:
    """Daemon-state names written as bare strings outside `DaemonState`.

    A `"ready"` used as a key into the health body (`body.get("ready")`, `body["ready"]`)
    is the wire field, not a state, and is allowed.
    """
    tree = ast.parse(inspect.getsource(module))
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DaemonState":
            allowed.update(id(child) for child in ast.walk(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            allowed.add(id(node.args[0]))
        elif isinstance(node, ast.Subscript):
            allowed.add(id(node.slice))
        elif isinstance(node, ast.Dict):
            allowed.update(id(key) for key in node.keys if key is not None)
    return [
        f"{module.__name__}:{node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in STATE_NAMES and id(node) not in allowed
    ]


@pytest.mark.parametrize("module", [daemon, connect], ids=["daemon", "connect"])
def test_no_daemon_state_string_literals_outside_the_enum(module):
    assert hasattr(daemon, "DaemonState")
    assert _state_literals(module) == []
