"""VCP's internal runtime broker against a real nats-server.

Scoped publish/subscribe/reply permissions (with a widened negative control),
credential stability across restart, revocation of new and existing
connections, hard VCP death, bounded startup failures, unmanaged listeners,
endpoint drift and reconnection after a socket cut. Uses the pinned
nats-server, the same discovery as `test_worktree_nats.py`. Runs on macOS and
Linux; no VM is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import nats
import pytest
from openengine.persistence import DatabaseConfig

from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.credentials import CredentialRows
from virtual_control_plane.provisioning import broker as broker_module
from virtual_control_plane.provisioning.broker import (
    BrokerEndpoint, BrokerUnavailable, CredentialRetired, EndpointDrift, RuntimeBroker, runtime_user,
)
from virtual_control_plane.runtime_nats import RuntimeUpdateListener
from virtual_control_plane.tests.test_lifecycle import broker as public_broker

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runtime broker runs on macOS and Linux")]

A, B = "rt_a", "rt_b"


@pytest.fixture
def database(tmp_path: Path):
    config = DatabaseConfig(sqlite_directory=tmp_path / "db")
    PERSISTENCE.upgrade(config)
    return config


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def make(tmp_path: Path, binary: Path, database: DatabaseConfig, port: int, **options) -> RuntimeBroker:
    endpoint = BrokerEndpoint("127.0.0.1", port, f"nats://127.0.0.1:{port}")
    return RuntimeBroker(tmp_path / "runtime-broker", binary, endpoint, CredentialRows(PERSISTENCE.database(database)), **options)


@asynccontextmanager
async def running(runtime_broker: RuntimeBroker):
    vcp = await runtime_broker.start()
    try:
        yield vcp
    finally:
        await vcp.close()
        await runtime_broker.close()


class Runtime:
    """A runtime's client, recording every permission violation the server reports."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.disconnected: list[float] = []

    async def connect(self, runtime_broker: RuntimeBroker, runtime_id: str, **options) -> nats.NATS:
        credential = runtime_broker.credential(runtime_id)

        async def error(exception: Exception) -> None:
            self.violations.append(str(exception))

        async def disconnected() -> None:
            self.disconnected.append(time.monotonic())
        options = {"allow_reconnect": False, "connect_timeout": 1, "max_reconnect_attempts": 0, **options}
        return await nats.connect(credential.url, user=credential.user, password=credential.path.read_text(),
                                  inbox_prefix=f"_INBOX.oe.runtime.{runtime_id}", error_cb=error,
                                  disconnected_cb=disconnected, **options)

    def denied(self, action: str, subject: str) -> bool:
        # nats-py lowercases the server's error.
        needle = f'permissions violation for {action} to "{subject}"'.lower()
        return any(needle in item.lower() for item in self.violations)


async def delivered(subscription, timeout: float = 0.5) -> bool:
    try:
        await subscription.next_msg(timeout=timeout)
    except nats.errors.TimeoutError:
        return False
    return True


async def scope_probe(runtime_broker: RuntimeBroker) -> dict[str, bool]:
    """What runtime A reaches: the server reported no violation, or a message got through."""
    async with running(runtime_broker) as vcp:
        await runtime_broker.issue(A, 1)
        await runtime_broker.issue(B, 1)
        a, b = Runtime(), Runtime()
        conn_a, conn_b = await a.connect(runtime_broker, A), await b.connect(runtime_broker, B)
        try:
            own_updates = await vcp.subscribe(f"oe.internal.v1.vcp.runtimes.{A}.updates")
            other_updates = await vcp.subscribe(f"oe.internal.v1.vcp.runtimes.{B}.updates")
            vcp_inbox = f"_INBOX.oe.vcp.{runtime_broker.instance}"
            subscriptions = {
                "own command": f"oe.internal.v1.runtime.runtimes.{A}.execute",
                "other command": f"oe.internal.v1.runtime.runtimes.{B}.execute",
                "every inbox": "_INBOX.>",
                "other inbox": f"_INBOX.oe.runtime.{B}.>",
                "vcp inbox": f"{vcp_inbox}.>",
                "public subscribe": "oe.v1.>",
            }
            subscribed = {name: await conn_a.subscribe(subject) for name, subject in subscriptions.items()}
            await conn_a.flush()

            async def answer(message) -> None:
                await message.respond(b"b")
            # B answers VCP on VCP's inbox; none of A's subscriptions may see it.
            await conn_b.subscribe(f"oe.internal.v1.runtime.runtimes.{B}.inspect", cb=answer)
            await conn_b.flush()
            assert (await vcp.request(f"oe.internal.v1.runtime.runtimes.{B}.inspect", b"?", timeout=2)).data == b"b"
            await vcp.publish(subscriptions["own command"], b"a")
            await vcp.publish(subscriptions["other command"], b"b")
            await vcp.flush()
            publications = {
                "own update": f"oe.internal.v1.vcp.runtimes.{A}.updates",
                "other update": f"oe.internal.v1.vcp.runtimes.{B}.updates",
                "publish command": f"oe.internal.v1.runtime.runtimes.{B}.execute",
                "public publish": "oe.v1.vcp.control.workspaces.apply",
                "forged reply": f"{vcp_inbox}.forged",
            }
            for subject in publications.values():
                await conn_a.publish(subject, b"x")
            await conn_a.flush()
            await asyncio.sleep(0.3)
            reached = {name: await delivered(subscription, 0.2) or not a.denied("Subscription", subscriptions[name])
                       for name, subscription in subscribed.items()}
            reached.update({name: not a.denied("Publish", subject) for name, subject in publications.items()})
            reached["own update"] &= await delivered(own_updates)
            reached["other update"] |= await delivered(other_updates, 0.2)
            return reached
        finally:
            await conn_a.close()
            await conn_b.close()


def test_runtime_credentials_reach_only_their_own_subjects_and_inbox(binary, tmp_path, database) -> None:
    access = asyncio.run(scope_probe(make(tmp_path, binary, database, free_port())))

    assert access == {name: name.startswith("own ") for name in access}


def test_the_scope_probe_detects_widened_permissions(binary, tmp_path, database, monkeypatch) -> None:
    """Negative control: the same probe reports every reach once A may use everything."""
    monkeypatch.setattr(broker_module, "runtime_permissions", lambda runtime_id: {
        "publish": {"allow": [">"]}, "subscribe": {"allow": [">"]}, "allow_responses": broker_module.RESPONSES})

    access = asyncio.run(scope_probe(make(tmp_path, binary, database, free_port())))

    assert all(access.values()), access


def test_a_received_request_permits_exactly_one_response(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker) as vcp:
            await runtime_broker.issue(A, 1)
            a = Runtime()
            connection = await a.connect(runtime_broker, A)
            replies: list[str] = []

            async def respond_twice(message) -> None:
                replies.append(message.reply)
                await message.respond(b"first")
                await message.respond(b"second")
            await connection.subscribe(f"oe.internal.v1.runtime.runtimes.{A}.execute", cb=respond_twice)
            await connection.flush()
            inbox = vcp.new_inbox()
            responses = await vcp.subscribe(inbox)
            await vcp.publish(f"oe.internal.v1.runtime.runtimes.{A}.execute", b"go", reply=inbox)
            await vcp.flush()

            assert (await responses.next_msg(timeout=2)).data == b"first"
            assert not await delivered(responses)
            assert a.denied("Publish", replies[0])
            await connection.close()

    asyncio.run(scenario())


def test_credentials_are_private_and_stable_across_broker_restart(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        port = free_port()
        first = make(tmp_path, binary, database, port)
        async with running(first):
            issued = await first.issue(A, 1)
            secret = issued.path.read_text()
            assert await first.issue(A, 1) == issued and issued.path.read_text() == secret
            argv = subprocess.run(["ps", "-ax", "-o", "args="], capture_output=True, text=True).stdout
            assert secret not in argv and str(first.config) in argv
            # The configuration holds bcrypt hashes, never a plaintext password.
            config = first.config.read_text()
            assert secret not in config and first._password not in config and "$2a$" in config
        directory = tmp_path / "runtime-broker"
        assert stat.S_IMODE(os.stat(directory / "credentials").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(issued.path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(first.config).st_mode) == 0o600
        row = await CredentialRows(PERSISTENCE.database(database)).get(A)
        assert row.status == "active" and row.secret_digest == broker_module.digest(secret) and secret not in str(row)

        second = make(tmp_path, binary, database, port)
        async with running(second):
            assert second.instance == first.instance
            assert (await second.issue(A, 1)).path.read_text() == secret
            await (await Runtime().connect(second, A)).close()
        assert secret not in (directory / "broker.log").read_text()

    asyncio.run(scenario())


def test_revocation_refuses_new_and_closes_existing_connections_and_survives_restart(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        port = free_port()
        rows = CredentialRows(PERSISTENCE.database(database))
        runtime_broker = make(tmp_path, binary, database, port)
        async with running(runtime_broker):
            credential = await runtime_broker.issue(A, 1)
            secret = credential.path.read_text()
            a = Runtime()
            connected = await a.connect(runtime_broker, A)
            started = time.monotonic()

            await runtime_broker.revoke(A)

            assert a.disconnected and a.disconnected[0] - started < broker_module.REVOCATION_CUTOFF_SECONDS
            assert not connected.is_connected
            assert (await rows.get(A)).status == "retired" and (await rows.get(A)).revoked_at is not None
            assert not credential.path.exists()
            with pytest.raises(nats.errors.Error, match="Authorization Violation"):
                await nats.connect(credential.url, user=credential.user, password=secret, allow_reconnect=False, connect_timeout=1)
            await runtime_broker.revoke(A)  # idempotent
            with pytest.raises(CredentialRetired):
                await runtime_broker.issue(A, 1)

        restarted = make(tmp_path, binary, database, port)
        async with running(restarted):
            with pytest.raises(nats.errors.Error, match="Authorization Violation"):
                await nats.connect(credential.url, user=credential.user, password=secret, allow_reconnect=False, connect_timeout=1)
            assert A not in restarted.config.read_text()

    asyncio.run(scenario())


def test_an_interrupted_revocation_is_never_restored_and_finishes_on_restart(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        port = free_port()
        rows = CredentialRows(PERSISTENCE.database(database))
        runtime_broker = make(tmp_path, binary, database, port)
        async with running(runtime_broker):
            await runtime_broker.issue(A, 1)
        # VCP stopped after marking the row and before confirming.
        await rows.transition(A, "active", "revoking")
        with pytest.raises(BrokerUnavailable):
            await runtime_broker.revoke(A)

        restarted = make(tmp_path, binary, database, port)
        async with running(restarted):
            assert (await rows.get(A)).status == "retired"
            assert not (restarted.credentials / A).exists()

    asyncio.run(scenario())


class Vcp:
    def __init__(self, tmp_path: Path, public: str, binary: Path, port: int) -> None:
        self.tmp_path, self.ready = tmp_path, tmp_path / "vcp.ready"
        self.environment = {**os.environ, "OE_NATS_URL": public, "OE_VCP_STATE_DIR": str(tmp_path / "vcp"),
                            "OE_VCP_READY_FILE": str(self.ready), "OE_VCP_HEALTH_PORT": "0",
                            "OE_DATABASE_BACKEND": "sqlite", "OE_SQLITE_DIRECTORY": str(tmp_path / "db"),
                            "OE_VCP_RUNTIME_BROKER_LISTEN": f"127.0.0.1:{port}",
                            "OE_VCP_RUNTIME_BROKER_BINARY": str(binary)}
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.ready.unlink(missing_ok=True)
        log = (self.tmp_path / "vcp.log").open("a")
        self.process = subprocess.Popen([sys.executable, "-m", "virtual_control_plane"], env=self.environment,
                                        stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while not self.ready.exists():
            assert self.process.poll() is None, (self.tmp_path / "vcp.log").read_text()
            assert time.monotonic() < deadline, "vcp did not become ready"
            time.sleep(0.05)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            assert self.process.wait(timeout=15) == 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A reaped-later zombie is not a running broker.
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(status) and not status.startswith("Z")


def test_vcp_sigkill_leaves_no_broker_port_or_lock_and_restart_reuses_the_endpoint(binary, tmp_path, database) -> None:
    port = free_port()
    directory = tmp_path / "vcp" / "runtime-broker"

    async def scenario() -> None:
        # Issued while VCP is down: the next launch's configuration grants it.
        offline = RuntimeBroker(directory, binary, BrokerEndpoint("127.0.0.1", port, f"nats://127.0.0.1:{port}"),
                                CredentialRows(PERSISTENCE.database(database)))
        secret = (await offline.issue(A, 1)).path.read_text()
        async with public_broker(binary) as connection:
            vcp = Vcp(tmp_path, connection.connected_url.geturl(), binary, port)
            try:
                vcp.start()
                await (await nats.connect(f"nats://127.0.0.1:{port}", user=runtime_user(A), password=secret, allow_reconnect=False)).close()
                ports = next((directory / "run").glob("*/*.ports"))
                server = int(ports.stem.rsplit("_", 1)[1])
                assert alive(server)

                vcp.process.kill()
                vcp.process.wait()
                deadline = time.monotonic() + 10
                while alive(server):
                    assert time.monotonic() < deadline, "nats-server outlived VCP"
                    await asyncio.sleep(0.05)
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
                with open(directory / "broker.lock", "a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                endpoint = (directory / "endpoint.json").read_text()

                vcp.start()
                assert (directory / "endpoint.json").read_text() == endpoint
                await (await nats.connect(f"nats://127.0.0.1:{port}", user=runtime_user(A), password=secret, allow_reconnect=False)).close()
                assert secret not in (tmp_path / "vcp.log").read_text()
            finally:
                vcp.stop()

    asyncio.run(scenario())


def fake_server(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-nats-server"
    path.write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'fake.pid'}\n{body}\n")
    path.chmod(0o755)
    return path


def test_a_server_that_exits_fails_startup_promptly(tmp_path, database) -> None:
    runtime_broker = make(tmp_path, fake_server(tmp_path, "exit 3"), database, free_port())
    started = time.monotonic()

    with pytest.raises(BrokerUnavailable, match="exited during startup"):
        asyncio.run(runtime_broker.start())

    assert time.monotonic() - started < broker_module.STARTUP_SECONDS


def test_an_unresponsive_server_fails_at_the_deadline_and_is_reaped(tmp_path, database) -> None:
    runtime_broker = make(tmp_path, fake_server(tmp_path, "exec sleep 600"), database, free_port(), startup_seconds=1.0)
    started = time.monotonic()

    with pytest.raises(BrokerUnavailable, match="did not start within"):
        asyncio.run(runtime_broker.start())

    assert time.monotonic() - started < 1.0 + broker_module.CLOSE_SECONDS
    assert not alive(int((tmp_path / "fake.pid").read_text()))


def test_an_occupied_port_fails_and_the_foreign_listener_is_left_alone(binary, tmp_path, database) -> None:
    port = free_port()
    foreign = subprocess.Popen([sys.executable, "-c", (
        "import socket, time\n"
        f"s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen()\n"
        "print('listening', flush=True)\n"
        "while True:\n    c, _ = s.accept(); c.sendall(b'INFO {\"server_name\": \"foreign\"}\\r\\n'); c.close()\n")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert foreign.stdout.readline().strip() == "listening"
        started = time.monotonic()

        with pytest.raises(BrokerUnavailable):
            asyncio.run(make(tmp_path, binary, database, port).start())

        assert time.monotonic() - started < broker_module.STARTUP_SECONDS
        assert foreign.poll() is None
        with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
            assert b"foreign" in client.recv(100)
    finally:
        foreign.kill()
        foreign.wait()


def test_a_changed_endpoint_is_refused(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        async with running(make(tmp_path, binary, database, free_port())):
            pass
        with pytest.raises(EndpointDrift):
            await make(tmp_path, binary, database, free_port()).start()

    asyncio.run(scenario())


def test_a_cut_socket_reconnects(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        sockets: list[asyncio.StreamWriter] = []

        async def pipe(reader, writer) -> None:
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()

        async def forward(client_reader, client_writer) -> None:
            server_reader, server_writer = await asyncio.open_connection("127.0.0.1", runtime_broker.endpoint.port)
            sockets.extend((client_writer, server_writer))
            await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer))
        proxy = await asyncio.start_server(forward, "127.0.0.1", 0)
        async with running(runtime_broker):
            reconnected = asyncio.Event()

            async def on_reconnect() -> None:
                reconnected.set()
            vcp = await nats.connect(f"nats://127.0.0.1:{proxy.sockets[0].getsockname()[1]}",
                                     **{**runtime_broker.vcp_options(), "reconnect_time_wait": 0.1},
                                     reconnected_cb=on_reconnect)
            for writer in sockets:
                writer.transport.abort()
            await asyncio.wait_for(reconnected.wait(), 10)
            await vcp.publish("oe.internal.v1.runtime.runtimes.rt_x.inspect", b"after")
            await vcp.flush(timeout=2)
            await vcp.close()
        proxy.close()

    asyncio.run(scenario())


def test_runtime_ids_must_be_single_subject_tokens(binary, tmp_path, database) -> None:
    runtime_broker = make(tmp_path, binary, database, free_port())
    for runtime_id in ("rt.a", "rt_*", ">", "rt a", ""):
        with pytest.raises(ValueError):
            asyncio.run(runtime_broker.issue(runtime_id, 1))


def test_runtime_permissions_are_exactly_the_contract_block() -> None:
    assert broker_module.runtime_permissions("rt_demo") == {
        "publish": {"allow": ["oe.internal.v1.vcp.runtimes.rt_demo.updates"]},
        "subscribe": {"allow": ["oe.internal.v1.runtime.runtimes.rt_demo.execute",
                                "oe.internal.v1.runtime.runtimes.rt_demo.cancel",
                                "oe.internal.v1.runtime.runtimes.rt_demo.steer",
                                "oe.internal.v1.runtime.runtimes.rt_demo.inspect",
                                "_INBOX.oe.runtime.rt_demo.>"]},
        "allow_responses": {"max": 1, "expires": "10s"},
    }
    assert json.dumps(broker_module.vcp_permissions("i1")).count("oe.v1") == 0


class Acknowledging:
    async def receive(self, subject: str, data: bytes) -> bytes:
        return b"ack"


def test_vcp_answers_an_update_only_on_its_senders_own_inbox(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker) as vcp:
            listener = RuntimeUpdateListener(vcp, Acknowledging())
            await listener.start()
            await runtime_broker.issue(A, 1)
            await runtime_broker.issue(B, 1)
            conn_a, conn_b = await Runtime().connect(runtime_broker, A), await Runtime().connect(runtime_broker, B)
            try:
                victims = [f"oe.internal.v1.runtime.runtimes.{B}.execute", f"_INBOX.oe.runtime.{B}.forged"]
                received = [await conn_b.subscribe(subject) for subject in victims]
                await conn_b.flush()
                for victim in victims:
                    await conn_a.publish(f"oe.internal.v1.vcp.runtimes.{A}.updates", b"u", reply=victim)
                await conn_a.flush()
                # Its own inbox is still answered, after the forged replies were dropped.
                assert (await conn_a.request(f"oe.internal.v1.vcp.runtimes.{A}.updates", b"u", timeout=2)).data == b"ack"
                assert not any([await delivered(subscription, 0.3) for subscription in received])
            finally:
                await conn_a.close()
                await conn_b.close()
                await listener.close()

    asyncio.run(scenario())


def test_a_runtime_named_vcp_gets_its_own_user(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker) as vcp:
            credential = await runtime_broker.issue("vcp", 1)
            assert credential.user == "runtime-vcp"
            await (await Runtime().connect(runtime_broker, "vcp")).close()
            await vcp.publish("oe.internal.v1.runtime.runtimes.vcp.inspect", b"?")
            await vcp.flush(timeout=2)

    asyncio.run(scenario())


def test_wildcard_listen_addresses_are_refused_in_every_spelling_and_ipv6_is_bracketed() -> None:
    for host in ("0.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0", "[::]", "::ffff:0.0.0.0"):
        with pytest.raises(ValueError, match="specific address"):
            BrokerEndpoint(host, 4300, "nats://192.0.2.1:4300")
    for host in ("", "localhost", "broker.internal"):
        with pytest.raises(ValueError, match="IP address"):
            BrokerEndpoint(host, 4300, "nats://192.0.2.1:4300")
    endpoint = BrokerEndpoint("0:0:0:0:0:0:0:1", 4300, "nats://[::1]:4300")
    assert endpoint.host == "::1" and endpoint.address == "[::1]:4300" and endpoint.client_url == "nats://[::1]:4300"


def test_a_crashed_broker_is_relaunched_and_still_revokes(binary, tmp_path, database, monkeypatch) -> None:
    monkeypatch.setattr(broker_module, "RESTART_SECONDS", (0.05, 0.2))

    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker) as vcp:
            credential = await runtime_broker.issue(A, 1)
            ports = next((runtime_broker.directory / "run").glob("*/*.ports"))
            os.kill(int(ports.stem.rsplit("_", 1)[1]), signal.SIGKILL)

            deadline = time.monotonic() + 15
            while ports.exists() or not (runtime_broker.running and vcp.is_connected):
                assert time.monotonic() < deadline, "the runtime broker was not relaunched"
                await asyncio.sleep(0.05)
            await vcp.publish(f"oe.internal.v1.runtime.runtimes.{A}.inspect", b"after")
            await vcp.flush(timeout=2)
            await (await Runtime().connect(runtime_broker, A)).close()
            await runtime_broker.revoke(A)
            with pytest.raises(nats.errors.Error, match="Authorization Violation"):
                await nats.connect(credential.url, user=credential.user, password="x", allow_reconnect=False)

    asyncio.run(scenario())


def kill_server(runtime_broker: RuntimeBroker) -> Path:
    """SIGKILL the running nats-server (not its owning child) and return its ports file."""
    ports = next((runtime_broker.directory / "run").glob("*/*.ports"))
    os.kill(int(ports.stem.rsplit("_", 1)[1]), signal.SIGKILL)
    return ports


def test_a_reconnect_never_sends_the_password_to_an_unverified_listener(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        port = free_port()
        runtime_broker = make(tmp_path, binary, database, port)
        async with running(runtime_broker) as vcp:
            password, name = runtime_broker._password, runtime_broker._server_name

            async def never(*_args) -> None:
                _ = broker_module._unavailable("relaunch held back by the test")
            runtime_broker._launch = never
            kill_server(runtime_broker)

            # An impostor takes the freed port and relays the real server's INFO.
            received, accepted = bytearray(), []

            async def impostor(reader, writer) -> None:
                accepted.append(True)
                writer.write(b"INFO " + json.dumps({"server_id": "x", "server_name": name, "version": "2.15.1",
                                                    "proto": 1, "max_payload": 1048576, "headers": True}).encode()
                             + b"\r\n")
                await writer.drain()
                with contextlib.suppress(OSError):
                    while data := await reader.read(4096):
                        received.extend(data)
                        writer.write(b"PONG\r\n")
                writer.close()
            deadline = time.monotonic() + 10
            while True:
                try:
                    server = await asyncio.start_server(impostor, "127.0.0.1", port)
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the killed server's port was not freed"
                    await asyncio.sleep(0.02)
            try:
                deadline = time.monotonic() + 10
                while len(accepted) < 2:
                    assert time.monotonic() < deadline, "VCP did not try to reconnect"
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.3)
            finally:
                server.close()
                server.close_clients()
            assert password.encode() not in received and b'"pass"' not in received
            assert not vcp.is_connected

    asyncio.run(scenario())


def test_a_relaunch_rotates_the_vcp_password_and_reverifies(binary, tmp_path, database, monkeypatch) -> None:
    monkeypatch.setattr(broker_module, "RESTART_SECONDS", (0.05, 0.2))

    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker) as vcp:
            before = runtime_broker._password
            ports = kill_server(runtime_broker)
            deadline = time.monotonic() + 15
            while ports.exists() or not (runtime_broker.running and vcp.is_connected):
                assert time.monotonic() < deadline, "the runtime broker was not relaunched"
                await asyncio.sleep(0.05)
            assert runtime_broker._password != before
            with pytest.raises(nats.errors.Error, match="Authorization Violation"):
                await nats.connect(runtime_broker.endpoint.client_url, user="vcp", password=before, allow_reconnect=False)

    asyncio.run(scenario())


def test_a_reload_racing_a_relaunch_is_retryable(binary, tmp_path, database, monkeypatch) -> None:
    monkeypatch.setattr(broker_module, "RESTART_SECONDS", (0.05, 0.2))

    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker) as vcp:
            gate, entered = asyncio.Event(), asyncio.Event()
            write_config = runtime_broker._write_config

            async def held(run: Path) -> None:
                entered.set()
                await gate.wait()
                await write_config(run)
            runtime_broker._write_config = held
            reload = asyncio.create_task(runtime_broker._reload())
            await entered.wait()
            child = runtime_broker._process
            kill_server(runtime_broker)
            await asyncio.wait_for(child.wait(), 10)
            await asyncio.sleep(0.2)  # the supervisor has noticed the exit and waits for the reload
            runtime_broker._write_config = write_config
            gate.set()
            with pytest.raises(BrokerUnavailable):
                await reload

            deadline = time.monotonic() + 15
            while not (runtime_broker.running and vcp.is_connected):
                assert time.monotonic() < deadline, "the runtime broker was not relaunched"
                await asyncio.sleep(0.05)
            await runtime_broker.issue(A, 1)

    asyncio.run(scenario())


def test_a_credential_probe_never_sends_the_secret_to_an_impostor(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        port = free_port()
        runtime_broker = make(tmp_path, binary, database, port)
        async with running(runtime_broker):
            credential = await runtime_broker.issue(A, 1)
            secret = credential.path.read_text()
            name, server_id = runtime_broker._server_name, runtime_broker._server_id
            received = bytearray()

            async def impostor(reader, writer) -> None:
                # A stale copy of the real INFO, from a listener that is not the live server.
                writer.write(b"INFO " + json.dumps({"server_id": server_id, "server_name": name, "version": "2.15.1",
                                                    "proto": 1, "max_payload": 1048576}).encode() + b"\r\n")
                await writer.drain()
                with contextlib.suppress(OSError):
                    while data := await reader.read(4096):
                        received.extend(data)
                        writer.write(b"PONG\r\n")
                writer.close()
            server = await asyncio.start_server(impostor, "127.0.0.1", 0)
            original = runtime_broker.endpoint
            runtime_broker.endpoint = BrokerEndpoint("127.0.0.1", server.sockets[0].getsockname()[1], credential.url)
            try:
                # Even while VCP still believes the endpoint verified, the probe's own socket is checked.
                runtime_broker._server_id = "a-different-live-server"
                with pytest.raises(BrokerUnavailable):
                    await runtime_broker._authenticates(credential.user, secret)
                runtime_broker._server_id = server_id
                runtime_broker._verified = False
                with pytest.raises(BrokerUnavailable):
                    await runtime_broker._authenticates(credential.user, secret)
            finally:
                runtime_broker.endpoint, runtime_broker._verified = original, True
                server.close()
                server.close_clients()
            assert secret.encode() not in received and b'"pass"' not in received

    asyncio.run(scenario())


def test_the_reverifier_keeps_retrying_after_any_verification_error(binary, tmp_path, database) -> None:
    async def scenario() -> None:
        runtime_broker = make(tmp_path, binary, database, free_port())
        async with running(runtime_broker):
            identified, failures = runtime_broker._identified, [TimeoutError(), ValueError("bad INFO")]

            async def flaky(run: Path) -> bool:
                if failures:
                    raise failures.pop(0)
                return await identified(run)
            runtime_broker._identified = flaky
            runtime_broker._verified = False
            await runtime_broker._disconnected()
            await asyncio.wait_for(runtime_broker._reverifier, 10)
            assert runtime_broker._verified and not failures

    asyncio.run(scenario())
