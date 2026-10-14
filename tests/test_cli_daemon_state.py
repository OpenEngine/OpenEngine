"""Daemon health as an explicit state machine (#776).

One `DaemonState` StrEnum, one pure function deriving it from the health
probe's outcome, and one mapping from state to exit code. Callers match on
the enum, so no state name appears as a string literal outside it.
"""

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
SPEC = daemon.ServiceSpec("/venv/bin/engine-web", "/config/engine.toml", 4364, "/logs/engine-web.log", {})


def openengine(**fields) -> bytes:
    """A `/api/health` body from a compatible, ready OpenEngine, with `fields` overridden."""
    return json.dumps({"service": "openengine", "api_version": daemon.API_VERSION, "ready": True, "version": "9.9.9", **fields}).encode()


def test_daemon_state_is_a_str_enum_of_the_five_health_states():
    assert issubclass(daemon.DaemonState, StrEnum)
    assert {state.name for state in daemon.DaemonState} == {name.upper() for name in STATE_NAMES}
    assert {state.value for state in daemon.DaemonState} == STATE_NAMES


# (probe outcome, state, whether the parsed body is kept, message)
OUTCOMES = [
    pytest.param({"error": URLError(ConnectionRefusedError(61, "Connection refused"))}, "DOWN", False,
                 f"cannot reach {URL}: [Errno 61] Connection refused", id="url-error"),
    pytest.param({"error": TimeoutError("timed out")}, "DOWN", False, f"cannot reach {URL}: timed out", id="timeout"),
    pytest.param({"error": OSError("network is unreachable")}, "DOWN", False,
                 f"cannot reach {URL}: network is unreachable", id="os-error"),
    pytest.param({"status": 503, "payload": openengine(ready=False)}, "STARTING", True,
                 "OpenEngine is starting or not ready", id="503-from-openengine"),
    pytest.param({"status": 503, "payload": json.dumps({"service": "nginx"}).encode()}, "FOREIGN", False,
                 "server returned HTTP 503", id="503-from-another-service"),
    pytest.param({"status": 503, "payload": b"<html>busy</html>"}, "FOREIGN", False,
                 "server returned HTTP 503", id="503-not-json"),
    pytest.param({"status": 503, "payload": None}, "FOREIGN", False, "server returned HTTP 503", id="503-unreadable-body"),
    pytest.param({"status": 404, "payload": openengine()}, "FOREIGN", False, "server returned HTTP 404", id="404"),
    pytest.param({"status": 500, "payload": b""}, "FOREIGN", False, "server returned HTTP 500", id="500"),
    pytest.param({"status": 200, "payload": b"<html>hello</html>"}, "FOREIGN", False,
                 "health endpoint did not return JSON", id="not-json"),
    pytest.param({"status": 200, "payload": b"[1, 2]"}, "FOREIGN", False,
                 "endpoint is not an OpenEngine service", id="json-array"),
    pytest.param({"status": 200, "payload": json.dumps({"service": "grafana", "ready": True}).encode()}, "FOREIGN", True,
                 "endpoint is not an OpenEngine service", id="another-service"),
    pytest.param({"status": 200, "payload": json.dumps({"ready": True}).encode()}, "FOREIGN", True,
                 "endpoint is not an OpenEngine service", id="no-service-field"),
    pytest.param({"status": 200, "payload": openengine(api_version=daemon.API_VERSION + 1)}, "INCOMPATIBLE", True,
                 f"unsupported API compatibility version: {daemon.API_VERSION + 1}", id="other-api-version"),
    pytest.param({"status": 200, "payload": openengine(api_version=None)}, "INCOMPATIBLE", True,
                 "unsupported API compatibility version: None", id="missing-api-version"),
    pytest.param({"status": 200, "payload": openengine(ready=False)}, "STARTING", True,
                 "OpenEngine is not ready", id="ready-false"),
    pytest.param({"status": 200, "payload": openengine(ready="true")}, "STARTING", True,
                 "OpenEngine is not ready", id="ready-not-literally-true"),
    pytest.param({"status": 200, "payload": openengine()}, "READY", True, "OpenEngine 9.9.9 is ready", id="ready"),
    pytest.param({"status": 200, "payload": json.dumps({"service": "openengine", "api_version": daemon.API_VERSION, "ready": True}).encode()},
                 "READY", True, "OpenEngine unknown is ready", id="ready-without-version"),
]


@pytest.mark.parametrize(("outcome", "state", "keeps_body", "message"), OUTCOMES)
def test_derive_state_maps_each_probe_outcome_to_a_state(monkeypatch, outcome, state, keeps_body, message):
    monkeypatch.setattr(daemon, "urlopen", lambda *_args, **_kwargs: pytest.fail("deriving a state must not probe the network"))

    derived, body, detail = daemon.derive_state(URL, **outcome)

    assert derived is daemon.DaemonState[state]
    assert body == (json.loads(outcome["payload"]) if keeps_body else None)
    assert detail == message


def test_probe_outcomes_reach_every_state():
    assert {param.values[1] for param in OUTCOMES} == {state.name for state in daemon.DaemonState}


class Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self.payload


def raises(error: BaseException):
    def respond(*_args, **_kwargs):
        raise error
    return respond


@pytest.mark.parametrize(("urlopen", "state"), [
    (lambda *_a, **_k: Response(openengine()), "READY"),
    (lambda *_a, **_k: Response(openengine(ready=False)), "STARTING"),
    (lambda *_a, **_k: Response(openengine(api_version=daemon.API_VERSION + 1)), "INCOMPATIBLE"),
    (raises(HTTPError(f"{URL}/api/health", 503, "unavailable", {}, BytesIO(openengine(ready=False)))), "STARTING"),
    (raises(HTTPError(f"{URL}/api/health", 404, "not found", {}, BytesIO(b""))), "FOREIGN"),
    (lambda *_a, **_k: Response(b"<html></html>"), "FOREIGN"),
    (raises(URLError(ConnectionRefusedError(61, "Connection refused"))), "DOWN"),
])
def test_check_health_returns_daemon_state_members(monkeypatch, urlopen, state):
    monkeypatch.setattr(daemon, "urlopen", urlopen)

    derived, _body, _detail = daemon.check_health(URL)

    assert derived is daemon.DaemonState[state]


@pytest.mark.parametrize(("ready", "state"), [(True, "READY"), (False, "STARTING")])
def test_health_folds_an_incompatible_openengine_into_ready_or_starting(monkeypatch, ready, state):
    body = json.loads(openengine(api_version=daemon.API_VERSION + 1, ready=ready))
    monkeypatch.setattr(daemon, "check_health", lambda _url, _timeout=2.0: (daemon.DaemonState.INCOMPATIBLE, body, "unsupported"))

    assert daemon.health(URL) == (daemon.DaemonState[state], body)
    assert daemon.health(URL)[0] is daemon.DaemonState[state]


def test_exit_codes_come_from_one_mapping_over_every_state():
    assert set(daemon.EXIT_CODES) == set(daemon.DaemonState)
    assert daemon.EXIT_CODES[daemon.DaemonState.READY] == daemon.EXIT_OK
    assert {code for state, code in daemon.EXIT_CODES.items() if state is not daemon.DaemonState.READY} == {daemon.EXIT_FAILED}


@pytest.mark.parametrize("state", ["DOWN", "FOREIGN", "READY", "STARTING"])
def test_status_exits_with_the_code_mapped_to_its_state(monkeypatch, capsys, state):
    monkeypatch.setattr(daemon, "current", lambda: (daemon.ProcessBackend(), SPEC))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (daemon.DaemonState[state], None))
    # Distinct codes prove `engine daemon status` reads the mapping rather than its own comparison.
    monkeypatch.setattr(daemon, "EXIT_CODES", {member: 40 + index for index, member in enumerate(daemon.DaemonState)})

    assert daemon.command_status(argparse.Namespace(json=True)) == daemon.EXIT_CODES[daemon.DaemonState[state]]
    assert json.loads(capsys.readouterr().out)["health"] == state.lower()


class NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def waits(monkeypatch):
    """Records the states each `wait_for` asks for, answering with `waits.answer`."""
    class Waits(list):
        answer = daemon.DaemonState.READY

    recorded = Waits()

    class QuietBackend(daemon.ProcessBackend):
        def start(self, _spec):
            pass

        def stop(self):
            pass

    def wait_for(_url, wanted, _timeout, _backend=None):
        recorded.append(wanted)
        return recorded.answer, {}

    monkeypatch.setattr(daemon, "instance_lock", NullLock)
    monkeypatch.setattr(daemon, "current", lambda: (QuietBackend(), SPEC))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (daemon.DaemonState.READY, {}))
    monkeypatch.setattr(daemon, "wait_for", wait_for)
    return recorded


def test_start_waits_for_ready_or_foreign_members(waits):
    daemon.start_service()

    assert waits == [{daemon.DaemonState.READY, daemon.DaemonState.FOREIGN}]
    assert all(isinstance(state, daemon.DaemonState) for state in waits[0])


def test_stop_waits_for_down_or_foreign_members(waits):
    waits.answer = daemon.DaemonState.DOWN

    daemon.stop_service()

    assert waits == [{daemon.DaemonState.DOWN, daemon.DaemonState.FOREIGN}]
    assert all(isinstance(state, daemon.DaemonState) for state in waits[0])


@pytest.mark.parametrize("state", ["READY", "STARTING"])
def test_stop_fails_while_openengine_still_answers(waits, state):
    waits.answer = daemon.DaemonState[state]

    with pytest.raises(RuntimeError, match="still answering"):
        daemon.stop_service()


def test_connect_starts_the_registered_local_daemon_when_it_is_down(monkeypatch):
    answers = iter([(daemon.DaemonState.DOWN, None, "cannot reach"), (daemon.DaemonState.READY, {}, "ready")])
    started = []
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: next(answers))
    monkeypatch.setattr(connect.daemon, "read_record", lambda: object())
    monkeypatch.setattr(connect.daemon, "start_service", lambda: started.append(True))

    connect.ensure_ready(Backend("local", URL))

    assert started == [True]


@pytest.mark.parametrize("state", ["FOREIGN", "INCOMPATIBLE", "STARTING"])
def test_connect_refuses_a_service_that_is_not_ready(monkeypatch, state):
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: (daemon.DaemonState[state], None, f"{state} detail"))
    monkeypatch.setattr(connect.daemon, "start_service", lambda: pytest.fail("only a down daemon is started"))

    with pytest.raises(RuntimeError, match=f"{state} detail"):
        connect.ensure_ready(Backend("local", URL))


def state_literals(path: Path) -> list[str]:
    """State names written as string literals outside `class DaemonState`.

    Keys of the `/api/health` body, such as `body.get("ready")`, name a field
    rather than a state and are not counted.
    """
    tree = ast.parse(path.read_text())
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DaemonState":
            skipped.update(id(child) for child in ast.walk(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            skipped.add(id(node.args[0]))
        elif isinstance(node, ast.Subscript):
            skipped.add(id(node.slice))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skipped.add(id(node.value))  # docstrings
    found = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in STATE_NAMES and id(node) not in skipped
    ]
    return [f"{path.name}:{line}: {value!r}" for line, value in sorted(found)]


@pytest.mark.parametrize("module", [daemon, connect], ids=["daemon.py", "connect.py"])
def test_no_daemon_state_string_literals_outside_the_enum(module):
    assert state_literals(Path(module.__file__)) == []
