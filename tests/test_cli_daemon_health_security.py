"""`engine daemon status` against another program holding the daemon's port with HTTP error replies."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from engine.apps.cli import __main__ as cli
from engine.apps.cli import daemon

READY_BODY = json.dumps({"service": "openengine", "version": "6.6.6", "ready": True, "api_version": daemon.API_VERSION}).encode()


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ENGINE_CONFIG", raising=False)
    monkeypatch.setattr(daemon, "_node_version", lambda _node: (24, 21, 0))
    monkeypatch.setattr(daemon, "_node_installs", lambda: [])
    return tmp_path


@contextmanager
def squatter(reply: Callable[[socket.socket], None]) -> Iterator[int]:
    """Another program listening on loopback that answers every connection with `reply`."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    stopping = threading.Event()

    def serve() -> None:
        while not stopping.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.recv(65536)
                reply(connection)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stopping.set()
        thread.join()
        listener.close()


def _register(home: Path, port: int) -> None:
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"[server]\nport = {port}\n")
    daemon.prepare_directories()
    daemon.write_record(daemon.Record("process", daemon.ServiceSpec(str(home / "engine-web"), str(config), port, str(daemon.log_path()), {})))


@pytest.mark.parametrize("code", [401, 403, 502])
def test_status_does_not_report_ready_for_an_http_error_with_an_openengine_body(home: Path, capsys, code):
    def reply(connection: socket.socket) -> None:
        connection.sendall(
            f"HTTP/1.1 {code} Error\r\nContent-Type: application/json\r\nContent-Length: {len(READY_BODY)}\r\nConnection: close\r\n\r\n".encode()
            + READY_BODY
        )

    with squatter(reply) as port:
        _register(home, port)
        exit_code = cli.main(["daemon", "status", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert report["health"] == "foreign"
    assert exit_code == 1


def test_status_survives_a_peer_that_stalls_while_sending_an_error_body(home: Path, capsys):
    release = threading.Event()

    def reply(connection: socket.socket) -> None:
        # Headers promise a body that never arrives before the probe's timeout.
        connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Type: text/html\r\nContent-Length: 4096\r\n\r\n<html>")
        release.wait(5)

    try:
        with squatter(reply) as port:
            _register(home, port)
            exit_code = cli.main(["daemon", "status", "--json"])
            release.set()
    finally:
        release.set()

    report = json.loads(capsys.readouterr().out)
    assert report["health"] == "foreign"
    assert exit_code == 1
