"""Daemon health as an explicit state machine: one `DaemonState` enum, one pure derivation, one exit-code mapping."""

from __future__ import annotations

import argparse
import ast
import json
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from engine.apps.cli import connect, daemon
from engine.cli.backends import Backend

URL = "http://127.0.0.1:4364"
STATE_NAMES = {"down", "foreign", "incompatible", "ready", "starting"}
SOURCES = [Path(daemon.__file__), Path(connect.__file__)]


def _body(**fields) -> bytes:
    return json.dumps({"service": "openengine", "api_version": daemon.API_VERSION, "ready": True, "version": "9.9.9", **fields}).encode()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Deriving a state is pure: it never probes the network itself."""
    monkeypatch.setattr(daemon, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("derivation must not do I/O")))


def test_daemon_state_is_a_str_enum_of_exactly_the_five_health_states():
    assert issubclass(daemon.DaemonState, StrEnum)
    assert {member.name for member in daemon.DaemonState} == {"DOWN", "FOREIGN", "INCOMPATIBLE", "READY", "STARTING"}
    assert {member.value for member in daemon.DaemonState} == STATE_NAMES


# Every branch of the derivation, from the probe's outcome to (state, body, message).
DERIVATIONS = [
    pytest.param({"error": URLError(ConnectionRefusedError(61, "Connection refused"))},
                 "DOWN", None, f"cannot reach {URL}: [Errno 61] Connection refused", id="connection-refused"),
    pytest.param({"error": TimeoutError("timed out")},
                 "DOWN", None, f"cannot reach {URL}: timed out", id="timeout"),
    pytest.param({"error": OSError("network is unreachable")},
                 "DOWN", None, f"cannot reach {URL}: network is unreachable", id="os-error"),
    pytest.param({"status": 503, "payload": _body(ready=False)},
                 "STARTING", "body", "OpenEngine is starting or not ready", id="503-openengine"),
    pytest.param({"status": 503, "payload": json.dumps({"service": "nginx"}).encode()},
                 "FOREIGN", None, "server returned HTTP 503", id="503-other-service"),
    pytest.param({"status": 503, "payload": b"<html>busy</html>"},
                 "FOREIGN", None, "server returned HTTP 503", id="503-not-json"),
    pytest.param({"status": 503, "payload": None},
                 "FOREIGN", None, "server returned HTTP 503", id="503-unreadable"),
    pytest.param({"status": 404, "payload": _body()},
                 "FOREIGN", None, "server returned HTTP 404", id="404"),
    pytest.param({"status": 500, "payload": b""},
                 "FOREIGN", None, "server returned HTTP 500", id="500"),
    pytest.param({"status": 200, "payload": b"<html>hello</html>"},
                 "FOREIGN", None, "health endpoint did not return JSON", id="not-json"),
    pytest.param({"status": 200, "payload": b"[1, 2]"},
                 "FOREIGN", None, "endpoint is not an OpenEngine service", id="json-not-an-object"),
    pytest.param({"status": 200, "payload": json.dumps({"service": "grafana", "ready": True}).encode()},
                 "FOREIGN", "body", "endpoint is not an OpenEngine service", id="another-service"),
    pytest.param({"status": 200, "payload": json.dumps({"ready": True}).encode()},
                 "FOREIGN", "body", "endpoint is not an OpenEngine service", id="no-service-field"),
    pytest.param({"status": 200, "payload": _body(api_version=2)},
                 "INCOMPATIBLE", "body", "unsupported API compatibility version: 2", id="newer-api-version"),
    pytest.param({"status": 200, "payload": _body(api_version=None)},
                 "INCOMPATIBLE", "body", "unsupported API compatibility version: None", id="missing-api-version"),
    pytest.param({"status": 200, "payload": _body(ready=False)},
                 "STARTING", "body", "OpenEngine is not ready", id="not-ready"),
    pytest.param({"status": 200, "payload": _body(ready="yes")},
                 "STARTING", "body", "OpenEngine is not ready", id="ready-not-true"),
    pytest.param({"status": 200, "payload": _body()},
                 "READY", "body", "OpenEngine 9.9.9 is ready", id="ready"),
    pytest.param({"status": 200, "payload": json.dumps({"service": "openengine", "api_version": daemon.API_VERSION, "ready": True}).encode()},
                 "READY", "body", "OpenEngine unknown is ready", id="ready-without-version"),
]


@pytest.mark.parametrize(("outcome", "state", "body", "message"), DERIVATIONS)
def test_derive_state_maps_every_probe_outcome(outcome, state, body, message):
    derived, derived_body, derived_message = daemon.derive_state(URL, **outcome)

    assert derived is daemon.DaemonState[state]
    expected_body = json.loads(outcome["payload"]) if body == "body" else None
    assert derived_body == expected_body
    assert derived_message == message


def test_derivations_reach_every_state():
    assert {param.values[1] for param in DERIVATIONS} == {member.name for member in daemon.DaemonState}


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self.payload


@pytest.mark.parametrize(("respond", "state"), [
    (lambda: _Response(_body()), "READY"),
    (lambda: _Response(_body(api_version=2)), "INCOMPATIBLE"),
    (lambda: (_ for _ in ()).throw(HTTPError(f"{URL}/api/health", 503, "busy", {}, BytesIO(_body(ready=False)))), "STARTING"),
    (lambda: (_ for _ in ()).throw(HTTPError(f"{URL}/api/health", 404, "missing", {}, BytesIO(b""))), "FOREIGN"),
    (lambda: (_ for _ in ()).throw(URLError(ConnectionRefusedError(61, "Connection refused"))), "DOWN"),
])
def test_check_health_returns_daemon_state_members(monkeypatch, respond, state):
    monkeypatch.setattr(daemon, "urlopen", lambda *_args, **_kwargs: respond())

    derived, _body, _message = daemon.check_health(URL)

    assert derived is daemon.DaemonState[state]


@pytest.mark.parametrize(("ready", "state"), [(True, "READY"), (False, "STARTING")])
def test_health_treats_an_incompatible_openengine_as_ours_to_manage(monkeypatch, ready, state):
    body = json.loads(_body(api_version=2, ready=ready))
    monkeypatch.setattr(daemon, "check_health", lambda _url, timeout=2.0: (daemon.DaemonState.INCOMPATIBLE, body, "unsupported"))

    derived, derived_body = daemon.health(URL)

    assert derived is daemon.DaemonState[state]
    assert derived_body == body


def test_exit_codes_come_from_one_mapping_covering_every_state():
    assert set(daemon.EXIT_CODES) == set(daemon.DaemonState)
    assert daemon.EXIT_CODES[daemon.DaemonState.READY] == daemon.EXIT_OK
    for state in set(daemon.DaemonState) - {daemon.DaemonState.READY}:
        assert daemon.EXIT_CODES[state] == daemon.EXIT_FAILED


@pytest.mark.parametrize("state", ["DOWN", "FOREIGN", "READY", "STARTING"])
def test_status_exits_with_the_mapped_code_for_its_state(monkeypatch, capsys, state):
    spec = daemon.ServiceSpec("/venv/bin/engine-web", "/config/engine.toml", 4364, "/logs/engine-web.log", {})
    monkeypatch.setattr(daemon, "current", lambda: (daemon.ProcessBackend(), spec))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (daemon.DaemonState[state], None))
    monkeypatch.setattr(daemon, "EXIT_CODES", {member: 40 + index for index, member in enumerate(daemon.DaemonState)})

    result = daemon.command_status(argparse.Namespace(json=True))

    assert result == daemon.EXIT_CODES[daemon.DaemonState[state]]
    assert json.loads(capsys.readouterr().out)["health"] == state.lower()


def _record_waits(monkeypatch, answer: str) -> list[set]:
    waits: list[set] = []
    spec = daemon.ServiceSpec("/venv/bin/engine-web", "/config/engine.toml", 4364, "/logs/engine-web.log", {})

    class QuietBackend(daemon.ProcessBackend):
        def stop(self):
            pass

        def start(self, _spec):
            pass

    monkeypatch.setattr(daemon, "instance_lock", lambda: _NullLock())
    monkeypatch.setattr(daemon, "current", lambda: (QuietBackend(), spec))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (daemon.DaemonState.READY, {}))

    def wait_for(_url, wanted, _timeout, _backend=None):
        waits.append(wanted)
        return daemon.DaemonState[answer], {}

    monkeypatch.setattr(daemon, "wait_for", wait_for)
    return waits


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_start_waits_for_daemon_state_members(monkeypatch):
    waits = _record_waits(monkeypatch, "READY")

    daemon.start_service()

    assert waits == [{daemon.DaemonState.READY, daemon.DaemonState.FOREIGN}]
    assert all(isinstance(state, daemon.DaemonState) for state in waits[0])


def test_stop_waits_for_daemon_state_members(monkeypatch):
    waits = _record_waits(monkeypatch, "DOWN")

    daemon.stop_service()

    assert waits == [{daemon.DaemonState.DOWN, daemon.DaemonState.FOREIGN}]
    assert all(isinstance(state, daemon.DaemonState) for state in waits[0])


@pytest.mark.parametrize("state", ["READY", "STARTING"])
def test_stop_still_fails_while_openengine_answers(monkeypatch, state):
    _record_waits(monkeypatch, state)

    with pytest.raises(RuntimeError, match="still answering"):
        daemon.stop_service()


def test_connect_starts_the_registered_local_daemon_when_it_is_down(monkeypatch):
    states = iter([(daemon.DaemonState.DOWN, None, "cannot reach"), (daemon.DaemonState.READY, {}, "ready")])
    started = []
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: next(states))
    monkeypatch.setattr(connect.daemon, "read_record", lambda: object())
    monkeypatch.setattr(connect.daemon, "start_service", lambda: started.append(True))

    connect.ensure_ready(Backend("local", "http://127.0.0.1:4364"))

    assert started == [True]


@pytest.mark.parametrize("state", ["FOREIGN", "INCOMPATIBLE", "STARTING"])
def test_connect_refuses_a_service_that_is_not_ready(monkeypatch, state):
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: (daemon.DaemonState[state], None, f"{state} detail"))
    monkeypatch.setattr(connect.daemon, "start_service", lambda: (_ for _ in ()).throw(AssertionError("must not start")))

    with pytest.raises(RuntimeError, match=f"{state} detail"):
        connect.ensure_ready(Backend("local", "http://127.0.0.1:4364"))


def _state_literals(path: Path) -> list[str]:
    """Daemon state names spelled as string literals, outside the enum and outside JSON field lookups."""
    tree = ast.parse(path.read_text())
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DaemonState":
            allowed.update(id(child) for child in ast.walk(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            allowed.add(id(node.args[0]))  # body.get("ready") reads the health envelope's field
        elif isinstance(node, ast.Subscript):
            allowed.add(id(node.slice))
    return sorted(
        f"{path.name}:{node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in STATE_NAMES and id(node) not in allowed
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.name)
def test_no_daemon_state_string_literals_outside_the_enum(path):
    assert _state_literals(path) == []
