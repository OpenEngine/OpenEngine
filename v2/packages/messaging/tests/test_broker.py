"""A forged launch artifact cannot replace the broker's TLS identity."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from openengine.messaging import broker
from openengine.messaging.broker import BrokerTrust, connect, load_trust, open_private_directory


def manifest(tmp_path: Path) -> Path:
    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    path = private / "broker.json"
    path.write_text(json.dumps({
        "version": 1, "url": "nats://127.0.0.1:4222", "ca_pem": "certificate",
        "server_hostname": "localhost", "launch_id": "launch-a",
    }))
    path.chmod(0o600)
    return path


def test_private_manifest_loads(tmp_path: Path) -> None:
    assert load_trust(manifest(tmp_path)).launch_id == "launch-a"


@pytest.mark.parametrize("target", ["file", "directory"])
def test_shared_permissions_rejected(tmp_path: Path, target: str) -> None:
    path = manifest(tmp_path)
    (path if target == "file" else path.parent).chmod(0o755)
    with pytest.raises(PermissionError):
        load_trust(path)


@pytest.mark.parametrize("target", ["file", "ancestor"])
def test_symlinks_rejected(tmp_path: Path, target: str) -> None:
    path = manifest(tmp_path)
    link = path.parent / "alias" if target == "file" else tmp_path.resolve() / "alias"
    link.symlink_to(path if target == "file" else path.parent)
    with pytest.raises(OSError):
        load_trust(link if target == "file" else link / path.name)


def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    path.unlink()
    os.mkfifo(path, mode=0o600)
    with pytest.raises(PermissionError):
        load_trust(path)


@pytest.mark.parametrize("contents", ["x", "x" * 65537, '{}', '{"version":true}'])
def test_invalid_manifest_rejected(tmp_path: Path, contents: str) -> None:
    path = manifest(tmp_path)
    path.write_text(contents)
    with pytest.raises(ValueError):
        load_trust(path)


def test_private_directory_creation(tmp_path: Path) -> None:
    path = tmp_path.resolve() / "one" / "two"
    fd = open_private_directory(path, create=True)
    try:
        assert os.fstat(fd).st_mode & 0o777 == 0o700
    finally:
        os.close(fd)


@pytest.mark.parametrize("option", ["tls", "tls_hostname", "tls_handshake_first", "servers"])
def test_tls_options_cannot_override_launch_policy(option: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(connect("nats://127.0.0.1:4222", **{option: None}))


def test_tls_without_trust_and_endpoint_mismatch_are_rejected() -> None:
    with pytest.raises(ValueError):
        asyncio.run(connect("tls://127.0.0.1:4222"))
    trust = BrokerTrust("nats://127.0.0.1:4222", "pem", "localhost", "launch")
    with pytest.raises(ValueError):
        asyncio.run(connect("nats://127.0.0.1:4223", trust=trust))


def certificate(tmp_path: Path, name: str, *, expired: bool = False) -> tuple[str, ssl.SSLContext]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(now - datetime.timedelta(days=1) if expired else now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False).sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_path, key_path = tmp_path / f"{name}.pem", tmp_path / f"{name}.key"
    cert_path.write_text(pem)
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return pem, context


@pytest.mark.parametrize("mode", [
    "valid", "wrong-ca", "wrong-host", "expired", "plaintext", "no-pong",
    "verification-disabled", "handshake-disabled",
])
def test_connection_authenticates_before_nats_protocol(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem, server_context = certificate(tmp_path, "broker", expired=mode == "expired")
    wrong_pem, _ = certificate(tmp_path, "other-launch")
    received: list[bytes] = []
    original_connect = broker._connect_owned

    async def weakened_connect(url: str, **options):
        # Negative controls establish that the impostors are usable brokers
        # when the corresponding authentication mechanism is removed.
        if mode == "verification-disabled":
            options["tls"].check_hostname = False
            options["tls"].verify_mode = ssl.CERT_NONE
        elif mode == "handshake-disabled":
            options["tls_handshake_first"] = False
        return await original_connect(url, **options)

    if mode in {"verification-disabled", "handshake-disabled"}:
        monkeypatch.setattr(broker, "_connect_owned", weakened_connect)

    async def exercise() -> None:
        tasks: set[asyncio.Task] = set()

        async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            tasks.add(task)
            try:
                # A plaintext impostor explicitly denies TLS to catch downgrade.
                writer.write(b'INFO {"server_id":"impostor","tls_required":false}\r\n')
                await writer.drain()
                while line := await reader.readline():
                    received.append(line)
                    if line == b"PING\r\n" and mode != "no-pong":
                        writer.write(b"PONG\r\n")
                        await writer.drain()
            except (ConnectionError, ssl.SSLError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, ssl.SSLError):
                    pass
                tasks.discard(task)

        server = await asyncio.start_server(
            peer, "127.0.0.1", 0, ssl=None if mode in {"plaintext", "handshake-disabled"} else server_context,
        )
        port = server.sockets[0].getsockname()[1]
        url = f"nats://127.0.0.1:{port}"
        trust = BrokerTrust(url, wrong_pem if mode in {"wrong-ca", "verification-disabled"} else pem,
                            "wrong.example" if mode == "wrong-host" else "localhost", "launch")
        try:
            operation = connect(url, trust=trust, allow_reconnect=False, connect_timeout=0.2)
            if mode in {"valid", "verification-disabled", "handshake-disabled"}:
                client = await asyncio.wait_for(operation, 1)
                await client.close()
                assert any(line.startswith(b"CONNECT ") for line in received)
            else:
                with pytest.raises(Exception):
                    await asyncio.wait_for(operation, 1)
                if mode != "no-pong":
                    assert not any(line.startswith((b"CONNECT ", b"PUB ")) for line in received)
        finally:
            server.close()
            await server.wait_closed()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_reconnect_retains_launch_identity_after_genuine_restart_and_takeover(tmp_path: Path) -> None:
    trusted_pem, trusted_context = certificate(tmp_path, "this-launch")
    _, sibling_context = certificate(tmp_path, "sibling-launch")

    async def exercise() -> None:
        writers: set[asyncio.StreamWriter] = set()
        tasks: set[asyncio.Task] = set()
        received: dict[str, list[bytes]] = {"initial": [], "restarted": [], "impostor": []}
        reconnected = asyncio.Event()
        rejected = asyncio.Event()

        async def on_reconnected() -> None:
            reconnected.set()

        async def on_error(error: Exception) -> None:
            if isinstance(error, ssl.SSLCertVerificationError):
                rejected.set()

        def peer(label: str):
            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                task = asyncio.current_task()
                tasks.add(task)
                writers.add(writer)
                try:
                    writer.write(b'INFO {"server_id":"peer","tls_required":true}\r\n')
                    await writer.drain()
                    while line := await reader.readline():
                        received[label].append(line)
                        if line == b"PING\r\n":
                            writer.write(b"PONG\r\n")
                            await writer.drain()
                except (ConnectionError, ssl.SSLError):
                    pass
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except (ConnectionError, ssl.SSLError):
                        pass
                    writers.discard(writer)
                    tasks.discard(task)
            return handle

        server = await asyncio.start_server(peer("initial"), "127.0.0.1", 0, ssl=trusted_context)
        port = server.sockets[0].getsockname()[1]
        url = f"nats://127.0.0.1:{port}"
        client = await connect(
            url, trust=BrokerTrust(url, trusted_pem, "localhost", "launch"),
            reconnect_time_wait=0.05, max_reconnect_attempts=5, connect_timeout=0.2,
            error_cb=on_error, reconnected_cb=on_reconnected,
        )
        try:
            # Replace only the listener first, then disconnect existing clients.
            # The reconnect must encounter the replacement at the same address.
            previous = server
            previous.close()
            server = await asyncio.start_server(peer("restarted"), "127.0.0.1", port, ssl=trusted_context)
            for writer in list(writers):
                writer.close()
            await asyncio.wait_for(previous.wait_closed(), 1)
            await asyncio.wait_for(reconnected.wait(), 2)
            await client.flush(timeout=1)
            assert any(line.startswith(b"CONNECT ") for line in received["restarted"])
            assert client.is_connected

            previous = server
            previous.close()
            server = await asyncio.start_server(peer("impostor"), "127.0.0.1", port, ssl=sibling_context)
            for writer in list(writers):
                writer.close()
            await asyncio.wait_for(previous.wait_closed(), 1)
            await asyncio.wait_for(rejected.wait(), 2)
            assert not client.is_connected
            # No protocol data reaches the sibling broker, including CONNECT
            # credentials or publications queued while reconnecting.
            await client.publish("qualification.marker", b"must-not-leak")
            await asyncio.sleep(0.1)
            assert received["impostor"] == []
        finally:
            # A rejected reconnect can leave nats-py's close waiter pending.
            # Bound fixture cleanup; the authentication assertions above have
            # already established whether any protocol reached the impostor.
            try:
                await asyncio.wait_for(client.close(), 0.2)
            except TimeoutError:
                client._transport._io_writer.transport.abort()
            server.close()
            await server.wait_closed()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())

