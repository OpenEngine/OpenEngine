from __future__ import annotations

import json
import os
import ssl
import threading
import time
from unittest.mock import Mock
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509

from openengine.messaging.broker import load_trust
from openengine.supervisor.broker_launch import Launch
from openengine.supervisor import supervisor
from openengine.supervisor.processes.nats import install_nats_server, server_command
from openengine.supervisor.state import Status, read_state, state_path
from openengine.supervisor.readiness.broker import wait_for_broker
from openengine.supervisor.readiness.http import NotReady
from openengine.supervisor.shutdown.processes import stop


def test_launch_separates_public_trust_and_private_keys_and_preserves_data(tmp_path: Path) -> None:
    directory = tmp_path.resolve() / "state"
    directory.mkdir(mode=0o700)
    store = directory / "nats"
    store.mkdir()
    (store / "existing-data").write_text("preserve")
    with Launch(directory, "127.0.0.1", 4222) as first:
        trust = load_trust(first.manifest)
        assert trust == first.trust
        assert first.runtime.stat().st_mode & 0o777 == 0o700
        assert (first.runtime / "broker.key").stat().st_mode & 0o777 == 0o600
        record = first.manifest.read_text()
        assert "PRIVATE KEY" not in record
        assert "broker.key" not in record
        assert not (first.runtime / "ca.key").exists()
        cert = x509.load_pem_x509_certificate((first.runtime / "broker.pem").read_bytes())
        assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [trust.server_hostname]
        config = json.loads(first.config.read_text())
        assert config["tls"]["handshake_first"] is True
    assert not first.manifest.exists()
    assert not first.runtime.exists()
    assert (store / "existing-data").read_text() == "preserve"
    with Launch(directory, "127.0.0.1", 4222) as second:
        assert second.trust.ca_pem != trust.ca_pem
        assert second.trust.launch_id != trust.launch_id


def test_second_owner_cannot_replace_live_manifest(tmp_path: Path) -> None:
    directory = tmp_path.resolve() / "state"
    with Launch(directory, "127.0.0.1", 4222) as first:
        before = first.manifest.read_bytes()
        with pytest.raises(BlockingIOError):
            with Launch(directory, "127.0.0.1", 4222):
                pytest.fail("acquired another supervisor's lock")
        assert first.manifest.read_bytes() == before


def test_launch_rejects_unsafe_directory_and_symlink(tmp_path: Path) -> None:
    directory = tmp_path.resolve() / "state"
    directory.mkdir(mode=0o755)
    with pytest.raises(PermissionError):
        with Launch(directory, "127.0.0.1", 4222):
            pass
    directory.chmod(0o700)
    linked = tmp_path.resolve() / "linked"
    linked.symlink_to(directory)
    with pytest.raises(OSError):
        with Launch(linked, "127.0.0.1", 4222):
            pass


def test_probe_cancellation_precedes_connection_and_key_cleanup(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with Launch(tmp_path.resolve() / "state", "127.0.0.1", 4222) as launch:
            with pytest.raises(NotReady, match="shutdown requested"):
                wait_for_broker(child, launch.trust, timeout=1, cancelled=lambda: True)
    finally:
        stop(child)
    assert not launch.runtime.exists()


@pytest.fixture(scope="module")
def nats_binary() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "openengine"
    return install_nats_server(cache)


def test_real_broker_authentication(tmp_path: Path, nats_binary: Path) -> None:
    binary = nats_binary
    with socket.socket() as client_socket, socket.socket() as monitor_socket:
        client_socket.bind(("127.0.0.1", 0))
        monitor_socket.bind(("127.0.0.1", 0))
        port, monitor = client_socket.getsockname()[1], monitor_socket.getsockname()[1]
    directory = tmp_path.resolve() / "state"
    with Launch(directory, "127.0.0.1", port) as launch:
        with (tmp_path / "nats.log").open("wb") as log:
            child = subprocess.Popen(server_command(
                binary, host="127.0.0.1", port=port, monitor_port=monitor,
                store=directory / "nats", config=launch.config,
            ), stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_for_broker(child, launch.trust, timeout=3, cancelled=lambda: False)
                assert child.poll() is None
            finally:
                stop(child)


def _ports() -> tuple[int, int]:
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def test_forged_file_and_foreign_broker_never_start_services(tmp_path: Path, nats_binary: Path, monkeypatch) -> None:
    port, monitor = _ports()
    directory = tmp_path.resolve() / "managed"
    with Launch(tmp_path.resolve() / "foreign", "127.0.0.1", port) as foreign:
        with (tmp_path / "foreign.log").open("wb") as log:
            competing = subprocess.Popen(server_command(
                nats_binary, host="127.0.0.1", port=port, monitor_port=monitor,
                store=foreign.directory / "nats", config=foreign.config,
            ), stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_for_broker(competing, foreign.trust, timeout=5, cancelled=lambda: False)
                owned = []

                def delayed_child(*_):
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                    owned.append(child)
                    (directory / f"nats-server_{child.pid}.ports").write_text("forged readiness")
                    return child

                start_services = Mock()
                monkeypatch.setattr(supervisor.nats, "start", delayed_child)
                monkeypatch.setattr(supervisor.services, "start", start_services)
                monkeypatch.setattr(supervisor, "READY_TIMEOUT_SECONDS", 0.3)
                # The real supervisor owns cleanup. Only the intended child's
                # startup is delayed to expose the original readiness window.
                settings = supervisor.Settings(directory, tmp_path / "cache", "127.0.0.1", port, monitor, nats_binary)
                assert supervisor.run(settings) == 1
                start_services.assert_not_called()
                assert owned[0].poll() is not None
                assert competing.poll() is None
                assert read_state(state_path(directory)).status == Status.FAILED
                assert not (directory / "broker.json").exists()
            finally:
                stop(competing)


@pytest.mark.parametrize("cancel", [False, True])
def test_tls_handshake_stall_has_bounded_cleanup(tmp_path: Path, cancel: bool) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    accepted = threading.Event()
    release = threading.Event()

    def stall() -> None:
        with listener:
            with listener.accept()[0] as peer:
                peer.settimeout(2)
                assert peer.recv(4096)  # TLS ClientHello, not NATS CONNECT.
                accepted.set()
                release.wait(2)

    thread = threading.Thread(target=stall)
    thread.start()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with Launch(tmp_path.resolve() / "state", "127.0.0.1", listener.getsockname()[1]) as launch:
            began = time.monotonic()
            with pytest.raises(NotReady):
                wait_for_broker(child, launch.trust, timeout=0.3, cancelled=lambda: cancel and accepted.is_set())
            assert time.monotonic() - began < 2
            assert accepted.is_set()
    finally:
        release.set()
        thread.join(timeout=3)
        stop(child)
    assert not launch.runtime.exists()


def test_verified_tls_without_pong_never_becomes_ready(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    saw_ping = threading.Event()
    release = threading.Event()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with Launch(tmp_path.resolve() / "state", "127.0.0.1", listener.getsockname()[1]) as launch:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(launch.runtime / "broker.pem", launch.runtime / "broker.key")

        def no_pong() -> None:
            with listener:
                with context.wrap_socket(listener.accept()[0], server_side=True) as peer:
                    peer.settimeout(2)
                    peer.sendall(b'INFO {"server_id":"test","host":"127.0.0.1","port":4222,"max_payload":1048576,"tls_required":true}\r\n')
                    data = b""
                    while b"PING\r\n" not in data:
                        data += peer.recv(4096)
                    saw_ping.set()
                    release.wait(2)

        thread = threading.Thread(target=no_pong)
        thread.start()
        try:
            began = time.monotonic()
            with pytest.raises(NotReady):
                wait_for_broker(child, launch.trust, timeout=0.3, cancelled=lambda: False)
            assert time.monotonic() - began < 2
            assert saw_ping.is_set()
        finally:
            release.set()
            thread.join(timeout=3)
            stop(child)


@pytest.mark.parametrize("operation", ["unlink", "rmtree"])
def test_cleanup_error_releases_owner_lock(tmp_path: Path, monkeypatch, operation: str) -> None:
    directory = tmp_path.resolve() / "state"
    launch = Launch(directory, "127.0.0.1", 4222)
    launch.__enter__()
    lock_fd, directory_fd = launch._lock_fd, launch._directory_fd
    with monkeypatch.context() as patch:
        if operation == "unlink":
            patch.setattr(Path, "unlink", Mock(side_effect=PermissionError("unlink denied")))
        else:
            patch.setattr("openengine.supervisor.broker_launch.shutil.rmtree", Mock(side_effect=PermissionError("delete denied")))
        with pytest.raises(PermissionError):
            launch.__exit__(None, None, None)
    for fd in (lock_fd, directory_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
    # Lock ownership, not successful file deletion, gates the next launch.
    with Launch(directory, "127.0.0.1", 4222) as replacement:
        assert replacement.trust.launch_id != launch.trust.launch_id
