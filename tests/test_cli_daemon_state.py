"""`engine daemon` health as an explicit state machine: every probe outcome maps to one `DaemonState`."""

from __future__ import annotations

import ast
import io
import json
from email.message import Message
from enum import StrEnum
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from engine.apps.cli import connect, daemon

URL = "http://127.0.0.1:4364"
STATE_NAMES = {"down", "foreign", "incompatible", "ready", "starting"}
SOURCES = [Path(daemon.__file__), Path(connect.__file__)]


def _envelope(**fields) -> bytes:
    return json.dumps({"service": "openengine", "api_version": daemon.API_VERSION, "ready": True, "version": "1.2.3", **fields}).encode()


def _http_error(code: int, body: bytes = b"") -> HTTPError:
    return HTTPError(f"{URL}/api/health", code, "error", Message(), io.BytesIO(body))


def test_daemon_state_is_a_str_enum_with_the_reported_values():
    assert issubclass(daemon.DaemonState, StrEnum)
    assert {state.value for state in daemon.DaemonState} == STATE_NAMES
    # `engine daemon status --json` keeps printing the same words.
    assert json.dumps({"health": daemon.DaemonState.READY}) == '{"health": "ready"}'


UNVERSIONED = {"service": "openengine", "api_version": 1, "ready": True}
OTHER_API = json.loads(_envelope(api_version=99))
NOT_READY = json.loads(_envelope(ready=False))
NOT_TRUE = json.loads(_envelope(ready="yes"))


@pytest.mark.parametrize(
    ("outcome", "state", "body", "message"),
    [
        (_envelope(), "READY", json.loads(_envelope()), "OpenEngine 1.2.3 is ready"),
        (json.dumps(UNVERSIONED).encode(), "READY", UNVERSIONED, "OpenEngine unknown is ready"),
        (_envelope(ready=False), "STARTING", NOT_READY, "OpenEngine is not ready"),
        (_envelope(ready="yes"), "STARTING", NOT_TRUE, "OpenEngine is not ready"),
        (_envelope(api_version=99), "INCOMPATIBLE", OTHER_API, "unsupported API compatibility version: 99"),
        (json.dumps({"service": "other"}).encode(), "FOREIGN", {"service": "other"}, "endpoint is not an OpenEngine service"),
        (b"[1, 2]", "FOREIGN", None, "endpoint is not an OpenEngine service"),
        (b"<html>hello</html>", "FOREIGN", None, "health endpoint did not return JSON"),
        (_http_error(503, _envelope(ready=False)), "STARTING", NOT_READY, "OpenEngine is starting or not ready"),
        (_http_error(503, b"not json"), "FOREIGN", None, "server returned HTTP 503"),
        (_http_error(503, json.dumps({"service": "nginx"}).encode()), "FOREIGN", None, "server returned HTTP 503"),
        (_http_error(404), "FOREIGN", None, "server returned HTTP 404"),
        (_http_error(500, _envelope()), "FOREIGN", None, "server returned HTTP 500"),
        (URLError(ConnectionRefusedError("refused")), "DOWN", None, f"cannot reach {URL}: refused"),
        (TimeoutError("timed out"), "DOWN", None, f"cannot reach {URL}: timed out"),
        (OSError("reset"), "DOWN", None, f"cannot reach {URL}: reset"),
    ],
    ids=["ready", "ready-without-version", "ready-false", "ready-not-true", "unsupported-api-version",
         "non-openengine-body", "non-object-json", "non-json", "http-503-openengine", "http-503-non-json",
         "http-503-other-service", "http-404", "http-500", "unreachable", "timeout", "os-error"],
)
def test_derive_state_maps_each_probe_outcome(outcome, state, body, message):
    """The pure function takes the response bytes, or the error the probe raised, and the probed URL."""
    derived, derived_body, derived_message = daemon.derive_state(outcome, URL)
    assert derived is daemon.DaemonState[state]
    assert (derived_body, derived_message) == (body, message)


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (_envelope(), "READY"),
        (_envelope(ready=False), "STARTING"),
        (_envelope(api_version=99), "INCOMPATIBLE"),
        (b"nope", "FOREIGN"),
        (_http_error(503, _envelope(ready=False)), "STARTING"),
        (_http_error(404), "FOREIGN"),
        (URLError("refused"), "DOWN"),
    ],
)
def test_check_health_returns_the_derived_state(monkeypatch, outcome, state):
    def fake_urlopen(_request, timeout):
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(outcome)

    monkeypatch.setattr(daemon, "urlopen", fake_urlopen)
    derived, _body, _message = daemon.check_health(URL)
    assert derived is daemon.DaemonState[state]


@pytest.mark.parametrize(("ready", "state"), [(True, "READY"), (False, "STARTING")])
def test_health_treats_any_openengine_as_manageable(monkeypatch, ready, state):
    monkeypatch.setattr(daemon, "urlopen", lambda _request, timeout: io.BytesIO(_envelope(api_version=99, ready=ready)))
    derived, body = daemon.health(URL)
    assert derived is daemon.DaemonState[state]
    assert body["api_version"] == 99


def test_every_state_has_one_status_exit_code():
    assert set(daemon.EXIT_CODE_BY_STATE) == set(daemon.DaemonState)
    assert daemon.EXIT_CODE_BY_STATE[daemon.DaemonState.READY] == daemon.EXIT_OK
    for state in set(daemon.DaemonState) - {daemon.DaemonState.READY}:
        assert daemon.EXIT_CODE_BY_STATE[state] == daemon.EXIT_FAILED


def _field_names(tree: ast.AST) -> set[int]:
    """Ids of string constants that name a JSON field (`body.get("ready")`, `body["ready"]`), not a state."""
    names: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            names.add(id(node.args[0]))
        elif isinstance(node, ast.Subscript):
            names.add(id(node.slice))
    return names


def _enum_body(tree: ast.AST) -> set[int]:
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DaemonState":
            inside.update(id(child) for child in ast.walk(node))
    return inside


@pytest.mark.parametrize("source", SOURCES, ids=lambda path: path.name)
def test_no_daemon_state_string_literals_outside_the_enum(source: Path):
    tree = ast.parse(source.read_text())
    allowed = _field_names(tree) | _enum_body(tree)
    stray = [
        f"{source.name}:{node.lineno} {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in STATE_NAMES and id(node) not in allowed
    ]
    assert stray == []


def test_connect_starts_a_down_local_daemon_by_state(monkeypatch):
    states = iter([
        (daemon.DaemonState.DOWN, None, "cannot reach"),
        (daemon.DaemonState.READY, {}, "OpenEngine is ready"),
    ])
    started = []
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: next(states))
    monkeypatch.setattr(connect.daemon, "read_record", lambda: object())
    monkeypatch.setattr(connect.daemon, "start_service", lambda: started.append(True))
    backend = type("Backend", (), {"url": URL, "is_local": True})()
    connect.ensure_ready(backend)
    assert started == [True]
