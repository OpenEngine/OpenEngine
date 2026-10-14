"""Daemon health as an explicit state machine (#776): one enum, one pure probe -> state function."""

from __future__ import annotations

import ast
import io
import json
from enum import StrEnum
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from engine.apps.cli import connect, daemon

STATE_NAMES = {"down", "foreign", "incompatible", "ready", "starting"}


def _json(**body) -> bytes:
    return json.dumps(body).encode()


OPENENGINE = {"service": "openengine", "version": "1.2.3", "api_version": daemon.API_VERSION}


def test_daemon_state_is_a_str_enum_of_the_five_states():
    assert issubclass(daemon.DaemonState, StrEnum)
    assert {state.value for state in daemon.DaemonState} == STATE_NAMES
    # `engine daemon status --json` keeps printing the same words.
    assert daemon.DaemonState.READY == "ready"
    assert json.dumps(daemon.DaemonState.STARTING) == '"starting"'


# (HTTP status or None when the connection failed, raw response body or None when it could not be read)
PROBE_OUTCOMES = {
    "connection refused": (None, None, "DOWN"),
    "503 from a starting OpenEngine": (503, _json(service="openengine", ready=False), "STARTING"),
    "503 from an OpenEngine of another API version": (503, _json(service="openengine", api_version=99), "STARTING"),
    "503 from another program": (503, _json(service="nginx"), "FOREIGN"),
    "503 that is not JSON": (503, b"<html>Service Unavailable</html>", "FOREIGN"),
    "503 whose body could not be read": (503, None, "FOREIGN"),
    "503 with a JSON list": (503, b"[]", "FOREIGN"),
    "404": (404, _json(**OPENENGINE, ready=True), "FOREIGN"),
    "500": (500, None, "FOREIGN"),
    "200 that is not JSON": (200, b"<html>hello</html>", "FOREIGN"),
    "200 with an empty body": (200, b"", "FOREIGN"),
    "200 with a JSON list": (200, b"[1, 2]", "FOREIGN"),
    "200 from another service": (200, _json(service="nginx", ready=True), "FOREIGN"),
    "200 without a service name": (200, _json(ready=True, api_version=daemon.API_VERSION), "FOREIGN"),
    "200 with an unsupported api_version": (200, _json(service="openengine", api_version=99, ready=True), "INCOMPATIBLE"),
    "200 with no api_version": (200, _json(service="openengine", ready=True), "INCOMPATIBLE"),
    "200 incompatible and not ready": (200, _json(service="openengine", api_version=99, ready=False), "INCOMPATIBLE"),
    "200 not ready": (200, _json(**OPENENGINE, ready=False), "STARTING"),
    "200 without ready": (200, _json(**OPENENGINE), "STARTING"),
    "200 with ready as a truthy string": (200, _json(**OPENENGINE, ready="true"), "STARTING"),
    "200 ready": (200, _json(**OPENENGINE, ready=True), "READY"),
}


@pytest.mark.parametrize(("status", "payload", "expected"), PROBE_OUTCOMES.values(), ids=PROBE_OUTCOMES.keys())
def test_derive_state_maps_every_probe_outcome(status, payload, expected):
    state = daemon.derive_state(status, payload)

    assert state is daemon.DaemonState[expected]


def test_derive_state_covers_every_state():
    reached = {daemon.derive_state(status, payload) for status, payload, _expected in PROBE_OUTCOMES.values()}

    assert reached == set(daemon.DaemonState)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _urlopen(status: int | None, payload: bytes | None):
    def opener(request, timeout):
        if status is None:
            raise URLError(ConnectionRefusedError(61, "Connection refused"))
        if status != 200:
            raise HTTPError(request.full_url, status, "error", {}, io.BytesIO(payload or b""))
        return _Response(payload or b"")
    return opener


@pytest.mark.parametrize(("status", "payload", "expected"), PROBE_OUTCOMES.values(), ids=PROBE_OUTCOMES.keys())
def test_check_health_returns_the_derived_state(monkeypatch, status, payload, expected):
    monkeypatch.setattr(daemon, "urlopen", _urlopen(status, payload))

    state, _body, detail = daemon.check_health("http://127.0.0.1:4364")

    assert state is daemon.DaemonState[expected]
    assert detail


@pytest.mark.parametrize(("ready", "expected"), [(True, "READY"), (False, "STARTING")])
def test_health_treats_any_openengine_as_ours_by_its_readiness(monkeypatch, ready, expected):
    payload = _json(service="openengine", api_version=99, ready=ready)
    monkeypatch.setattr(daemon, "urlopen", _urlopen(200, payload))

    state, body = daemon.health("http://127.0.0.1:4364")

    assert state is daemon.DaemonState[expected]
    assert body["api_version"] == 99


def test_exit_codes_come_from_one_mapping_over_every_state():
    assert set(daemon.EXIT_CODES) == set(daemon.DaemonState)
    assert daemon.EXIT_CODES[daemon.DaemonState.READY] == daemon.EXIT_OK
    for state in set(daemon.DaemonState) - {daemon.DaemonState.READY}:
        assert daemon.EXIT_CODES[state] == daemon.EXIT_FAILED


@pytest.mark.parametrize("name", sorted(STATE_NAMES))
def test_status_exits_by_the_mapping(monkeypatch, capsys, tmp_path, name):
    state = daemon.DaemonState(name)

    class Backend:
        name = "process"

        def running(self):
            return False

    spec = daemon.ServiceSpec("/bin/engine-web", str(tmp_path / "engine.toml"), 4364, str(tmp_path / "engine.log"), {})
    monkeypatch.setattr(daemon, "current", lambda: (Backend(), spec))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: (state, None))
    monkeypatch.setattr(daemon, "version", lambda _name: "0.0.0")

    assert daemon.command_status(daemon.argparse.Namespace(json=True)) == daemon.EXIT_CODES[state]
    assert json.loads(capsys.readouterr().out)["health"] == state.value


def _state_literals(path: Path) -> list[str]:
    """State words used as string literals, other than as health JSON keys (`body.get("ready")`) or in the enum."""
    tree = ast.parse(path.read_text())
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DaemonState":
            allowed.update(id(child) for child in ast.walk(node))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            allowed.add(id(node.args[0]))
        elif isinstance(node, ast.Subscript):
            allowed.add(id(node.slice))
    return [
        f"{path.name}:{node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in STATE_NAMES and id(node) not in allowed
    ]


@pytest.mark.parametrize("module", [daemon, connect], ids=["daemon.py", "connect.py"])
def test_no_daemon_state_string_literals_outside_the_enum(module):
    assert _state_literals(Path(module.__file__)) == []
