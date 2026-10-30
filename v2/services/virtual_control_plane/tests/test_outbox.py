"""The transactional outbox: events commit and roll back with lifecycle records,
and the publisher delivers each committed event at least once, in workspace
insertion order, with its original identity.

The unit tests use an in-memory publisher; the `integration` tests publish to a
real JetStream `vcp-events` stream created from `infra/nats/streams`.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import nats
import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from openengine.cli.migrations.streams import load_stream_configs, migrate_streams
from openengine.persistence import DatabaseConfig
from openengine.persistence.migrations import downgrade
from openengine.supervisor.processes.nats import install_nats_server

from virtual_control_plane.application.outbox import OutboxPublisher, PendingEvent, PublishFailed
from virtual_control_plane.application.transactions import InvalidEvent
from virtual_control_plane.jetstream import JetStreamEvents, UnaddressableEvent, event_subject
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.persistence.schema import metadata, outbox

STREAMS = Path(__file__).parents[3] / "infra" / "nats" / "streams"
SUBJECT = "oe.v1.vcp.control.workspaces.apply"


def _event(event_id: str, workspace_id: str = "ws_a1", command_id: str = "cmd_create_a") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": "workspace.provisioning",
        "occurred_at": "2026-10-19T21:30:00Z",
        "producer": "vcp",
        "causal_path": [command_id, event_id],
        "resources": {"workspace_id": workspace_id},
        "resource_version": 1,
        "payload": {"operation": {"operation_id": f"op_{command_id}", "command_id": command_id},
                    "provider": "process"},
    }


def _command(command_id: str = "cmd_create_a") -> dict[str, Any]:
    return {"command_id": command_id, "causal_path": [command_id],
            "payload": {"workspace": {"name": "example", "provider": "process"}}}


async def accept(store: DurableStore, command: dict[str, Any], event_id: str) -> dict[str, Any]:
    """A minimal producer: replay first, else commit state, receipt and event together."""
    async with store.transaction() as tx:
        if (original := await tx.replay(SUBJECT, command)) is not None:
            return original
        response = {"command_id": command["command_id"], "status": "accepted",
                    "operation_id": f"op_{command['command_id']}", "resource_id": "ws_a1"}
        await tx.put("workspaces", "ws_a1", {"id": "ws_a1", "version": 1, "state": "provisioning"})
        await tx.put("operations", response["operation_id"], {"state": "pending"})
        await tx.append_event(_event(event_id, command_id=command["command_id"]))
        return await tx.record_command(SUBJECT, command, response)


async def _rows(store: DurableStore) -> list[dict[str, Any]]:
    async with store.database.connect() as connection:
        return [dict(row) for row in (await connection.execute(sa.select(outbox).order_by(outbox.c.sequence))).mappings()]


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path / "db")
    PERSISTENCE.upgrade(config)
    return config


@asynccontextmanager
async def _store(config: DatabaseConfig) -> AsyncIterator[DurableStore]:
    store = DurableStore(PERSISTENCE.database(config))
    try:
        yield store
    finally:
        await store.close()


class Recording:
    """An in-memory `EventPublisher`; `fail` decides, per envelope, whether the ack is lost."""

    def __init__(self, fail: Callable[[dict[str, Any]], bool] = lambda event: False) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.acknowledged: list[dict[str, Any]] = []
        self.fail = fail

    async def publish(self, event: dict[str, Any]) -> None:
        self.attempts.append(json.loads(json.dumps(event)))
        if self.fail(event):
            raise TimeoutError  # The acknowledgement was lost.
        self.acknowledged.append(event)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failure_rolls_back_events_with_lifecycle_records(config: DatabaseConfig, failure: type[BaseException]) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            with pytest.raises(failure):
                async with store.transaction() as tx:
                    await tx.put("workspaces", "ws_a1", {"id": "ws_a1"})
                    await tx.put("operations", "op_1", {"state": "pending"})
                    await tx.put("workspace_runtimes", "ws_a1", {"runtime_id": "rt_1"})
                    await tx.append_event(_event("evt_1"))
                    await tx.record_command(SUBJECT, _command(), {"command_id": "cmd_create_a", "status": "accepted"})
                    raise failure()
            assert await store.pending_events(10) == []
            assert await _rows(store) == []
            async with store.transaction() as tx:
                assert await tx.replay(SUBJECT, _command()) is None
                for collection in ("workspaces", "operations", "workspace_runtimes"):
                    assert await tx.all(collection) == []

    asyncio.run(scenario())


def test_command_replay_appends_no_duplicate_event_even_after_restart(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            first = await accept(store, _command(), "evt_1")
            assert await accept(store, _command(), "evt_2") == first
        async with _store(config) as reopened:
            assert await accept(reopened, _command(), "evt_3") == first
            assert [event.event_id for event in await reopened.pending_events(10)] == ["evt_1"]

    asyncio.run(scenario())


def test_event_identity_is_unique_and_causal(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            async with store.transaction() as tx:
                await tx.append_event(_event("evt_1"))
            with pytest.raises(IntegrityError):
                async with store.transaction() as tx:
                    await tx.put("operations", "op_2", {"state": "pending"})
                    await tx.append_event(_event("evt_1", workspace_id="ws_b1"))
            unpublishable = [
                {**_event("evt_2"), "causal_path": ["cmd_create_a"]},
                {**_event("evt_2"), "causal_path": []},
                {key: value for key, value in _event("evt_2").items() if key != "resources"},
                {**_event("evt_2"), "event_type": "workspace.unknown"},
                _event("evt_2", workspace_id="ws.a1"),
                _event("evt_2\r\nNats-Expected-Stream: other"),
            ]
            for envelope in unpublishable:
                # Rejected with the producer's state, before it could hold back its workspace.
                with pytest.raises(InvalidEvent):
                    async with store.transaction() as tx:
                        await tx.put("operations", "op_2", {"state": "pending"})
                        await tx.append_event(envelope)
            async with store.transaction() as tx:
                assert await tx.all("operations") == []
            [pending] = await store.pending_events(10)
            assert pending.envelope == _event("evt_1") and pending.workspace_id == "ws_a1"

    asyncio.run(scenario())


def test_failed_event_is_not_overtaken_within_its_workspace(config: DatabaseConfig) -> None:
    # Event IDs sort opposite to insertion order; only `sequence` orders publication.
    inserted = [("evt_z", "ws_a1"), ("evt_y", "ws_b1"), ("evt_x", "ws_a1"), ("evt_w", "ws_b1")]

    async def scenario() -> None:
        async with _store(config) as store:
            for event_id, workspace_id in inserted:
                async with store.transaction() as tx:
                    await tx.append_event(_event(event_id, workspace_id))
            failing = {"evt_z"}
            publisher = Recording(lambda event: event["event_id"] in failing)
            worker = OutboxPublisher(store, publisher)
            with pytest.raises(PublishFailed):
                await worker.publish_pending()
            # ws_a1 is held behind evt_z; ws_b1 is independent.
            assert [event["event_id"] for event in publisher.attempts] == ["evt_z", "evt_y", "evt_w"]
            assert [event.event_id for event in await store.pending_events(10)] == ["evt_z", "evt_x"]
            failing.clear()
            assert await worker.publish_pending() == 2
            assert [event["event_id"] for event in publisher.acknowledged] == ["evt_y", "evt_w", "evt_z", "evt_x"]
            assert publisher.attempts[0] == publisher.attempts[3] == _event("evt_z")
            rows = await _rows(store)
            assert all(row["published_at"] is not None for row in rows)
            assert [row["event_id"] for row in rows] == [event_id for event_id, _ in inserted]

    asyncio.run(scenario())


def test_a_stuck_workspace_backlog_does_not_hide_other_workspaces(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            for index in range(5):
                async with store.transaction() as tx:
                    await tx.append_event(_event(f"evt_a{index}", "ws_a1"))
            async with store.transaction() as tx:
                await tx.append_event(_event("evt_b0", "ws_b1"))
            publisher = Recording(lambda event: event["resources"]["workspace_id"] == "ws_a1")
            worker = OutboxPublisher(store, publisher, batch_size=2)
            with pytest.raises(PublishFailed):
                await worker.publish_pending()
            # ws_a1's head is tried once; its backlog fills the first batch, not the pass.
            assert [event["event_id"] for event in publisher.attempts] == ["evt_a0", "evt_b0"]
            assert [event["event_id"] for event in publisher.acknowledged] == ["evt_b0"]
            assert [event.event_id for event in await store.pending_events(10)] == [f"evt_a{i}" for i in range(5)]

    asyncio.run(scenario())


class CrashBeforeMarking:
    """Wraps the store so recording publication fails once after the broker acknowledged."""

    def __init__(self, store: DurableStore) -> None:
        self.store, self.crashed = store, False

    async def pending_events(self, limit: int, exclude: Iterable[str] = ()) -> list[PendingEvent]:
        return await self.store.pending_events(limit, exclude)

    async def mark_published(self, sequence: int) -> None:
        if not self.crashed:
            self.crashed = True
            raise OSError  # Crashed before recording publication.
        await self.store.mark_published(sequence)


def test_acknowledgement_before_marker_crash_retries_the_same_event(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            async with store.transaction() as tx:
                await tx.append_event(_event("evt_1"))
            publisher = Recording()
            with pytest.raises(OSError):
                await OutboxPublisher(CrashBeforeMarking(store), publisher).publish_pending()
        async with _store(config) as reopened:
            assert await OutboxPublisher(reopened, publisher).publish_pending() == 1
            assert publisher.acknowledged == [_event("evt_1"), _event("evt_1")]
            assert await reopened.pending_events(10) == []

    asyncio.run(scenario())


def test_worker_backs_off_then_publishes_when_the_broker_recovers(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            async with store.transaction() as tx:
                await tx.append_event(_event("evt_1"))
            down = True
            publisher = Recording(lambda event: down)
            worker = OutboxPublisher(store, publisher, idle_interval=0.01, min_backoff=0.01, max_backoff=0.04)
            worker.start()
            try:
                await _until(lambda: len(publisher.attempts) >= 4)
                assert len(await store.pending_events(10)) == 1
                down = False
                await _until(lambda: publisher.acknowledged)
                await _until_async(lambda: store.pending_events(10), [])
            finally:
                await worker.close(timeout=1)
            assert {json.dumps(event) for event in publisher.attempts} == {json.dumps(_event("evt_1"))}

    asyncio.run(scenario())


def test_shutdown_cancels_in_flight_publish_and_leaves_it_recoverable(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            async with store.transaction() as tx:
                await tx.append_event(_event("evt_1"))
            entered = asyncio.Event()

            class Hanging:
                async def publish(self, event: dict[str, Any]) -> None:
                    entered.set()
                    await asyncio.Event().wait()

            worker = OutboxPublisher(store, Hanging())
            worker.start()
            await asyncio.wait_for(entered.wait(), timeout=2)
            started = time.monotonic()
            await worker.close(timeout=0.1)
            assert time.monotonic() - started < 1
            # Shutdown during a long backoff is prompt too.
            backing_off = OutboxPublisher(store, Recording(lambda event: True), min_backoff=60)
            backing_off.start()
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await backing_off.close(timeout=5)
            assert time.monotonic() - started < 1
        async with _store(config) as reopened:
            publisher = Recording()
            assert await OutboxPublisher(reopened, publisher).publish_pending() == 1
            assert publisher.acknowledged == [_event("evt_1")]

    asyncio.run(scenario())


def test_notify_wakes_an_idle_worker(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        async with _store(config) as store:
            publisher = Recording()
            worker = OutboxPublisher(store, publisher, idle_interval=60)
            worker.start()
            try:
                await asyncio.sleep(0.05)
                async with store.transaction() as tx:
                    await tx.append_event(_event("evt_1"))
                worker.notify()
                await _until(lambda: publisher.acknowledged)
            finally:
                await worker.close()

    asyncio.run(scenario())


def test_upgrade_from_lifecycle_schema_preserves_records(tmp_path: Path) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config, "ebfe415656b6")
    command = _command()

    async def seed() -> dict[str, Any]:
        async with _store(config) as store:
            async with store.transaction() as tx:
                await tx.put("workspaces", "ws_a1", {"id": "ws_a1", "version": 3})
                await tx.put("workspace_runtimes", "ws_a1", {"runtime_id": "rt_1", "generation": 2})
                return await tx.record_command(SUBJECT, command, {"command_id": "cmd_create_a", "status": "accepted"})

    async def check(response: dict[str, Any]) -> None:
        async with _store(config) as store:
            async with store.transaction() as tx:
                assert await tx.get("workspaces", "ws_a1") == {"id": "ws_a1", "version": 3}
                assert await tx.get("workspace_runtimes", "ws_a1") == {"runtime_id": "rt_1", "generation": 2}
                assert await tx.replay(SUBJECT, command) == response

    response = asyncio.run(seed())
    PERSISTENCE.upgrade(config)
    assert PERSISTENCE.drift(metadata, config) == []
    asyncio.run(check(response))
    database = PERSISTENCE.database(config)
    downgrade(PERSISTENCE.migrations, database, "ebfe415656b6")
    engine = database.migration_engine()
    try:
        assert "outbox" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    asyncio.run(check(response))


def test_event_subject_rejects_tokens_that_would_change_the_subject() -> None:
    assert event_subject(_event("evt_1")) == "oe.v1.vcp.events.workspaces.ws_a1.workspace.provisioning"
    for workspace_id in ("ws.a1", "ws*", ">", "ws a1", ""):
        with pytest.raises(UnaddressableEvent):
            event_subject(_event("evt_1", workspace_id))


async def _until(predicate: Callable[[], Any], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true before the deadline"
        await asyncio.sleep(0.01)


async def _until_async(read: Callable[[], Any], expected: Any, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while await read() != expected:
        assert time.monotonic() < deadline, "condition did not become true before the deadline"
        await asyncio.sleep(0.01)


# Real NATS/JetStream.


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Broker:
    """A JetStream-enabled nats-server that can be stopped and restarted on the same port and store."""

    def __init__(self, binary: Path, directory: Path) -> None:
        self.binary, self.directory = binary, directory
        self.port = _port()
        self.url = f"nats://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        log = (self.directory / "broker.log").open("a")
        self.process = subprocess.Popen(
            [str(self.binary), "-a", "127.0.0.1", "-p", str(self.port), "-js", "-sd", str(self.directory / "js")],
            stdout=log, stderr=subprocess.STDOUT,
        )
        log.close()
        deadline = time.monotonic() + 10
        while True:
            assert self.process.poll() is None, "broker exited"
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                assert time.monotonic() < deadline, "broker did not listen"
                time.sleep(0.05)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)


@pytest.fixture(scope="module")
def nats_binary() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "openengine"
    return install_nats_server(cache)


@pytest.fixture
def broker(tmp_path: Path, nats_binary: Path):
    broker = Broker(nats_binary, tmp_path)
    broker.start()
    asyncio.run(migrate_streams(broker.url, [c for c in load_stream_configs(STREAMS) if c["name"] == "vcp-events"]))
    try:
        yield broker
    finally:
        broker.stop()


async def _stream(url: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every message in vcp-events as (subject, Nats-Msg-Id, envelope), in stream order."""
    connection = await nats.connect(url)
    try:
        jetstream = connection.jetstream()
        info = await jetstream.stream_info("vcp-events")
        messages = []
        for sequence in range(info.state.first_seq, info.state.last_seq + 1):
            message = await jetstream.get_msg("vcp-events", sequence)
            messages.append((message.subject, message.headers["Nats-Msg-Id"], json.loads(message.data)))
        return messages
    finally:
        await connection.close()


class LosingFirstAck:
    """Publishes for real, but reports the first acknowledgement as lost."""

    def __init__(self, publisher: JetStreamEvents) -> None:
        self.publisher, self.lost = publisher, False

    async def publish(self, event: dict[str, Any]) -> None:
        await self.publisher.publish(event)
        if not self.lost:
            self.lost = True
            raise TimeoutError  # The acknowledgement was lost.


@pytest.mark.integration
def test_lost_acknowledgement_and_marker_crash_republish_the_same_identity(config: DatabaseConfig, broker: Broker) -> None:
    async def scenario() -> None:
        connection = await nats.connect(broker.url)
        try:
            jetstream = JetStreamEvents(connection.jetstream())
            async with _store(config) as store:
                async with store.transaction() as tx:
                    await tx.append_event(_event("evt_1"))
                    await tx.append_event(_event("evt_2"))
                with pytest.raises(PublishFailed):
                    await OutboxPublisher(store, LosingFirstAck(jetstream)).publish_pending()
                assert [event.event_id for event in await store.pending_events(10)] == ["evt_1", "evt_2"]
                with pytest.raises(OSError):
                    await OutboxPublisher(CrashBeforeMarking(store), jetstream).publish_pending()
            async with _store(config) as reopened:
                assert await OutboxPublisher(reopened, jetstream).publish_pending() == 2
                assert await reopened.pending_events(10) == []
        finally:
            await connection.close()
        # JetStream acknowledged every retry as a duplicate of the original message.
        assert await _stream(broker.url) == [
            ("oe.v1.vcp.events.workspaces.ws_a1.workspace.provisioning", "evt_1", _event("evt_1")),
            ("oe.v1.vcp.events.workspaces.ws_a1.workspace.provisioning", "evt_2", _event("evt_2")),
        ]

    asyncio.run(scenario())


@pytest.mark.integration
def test_broker_outage_preserves_pending_events_and_their_order(config: DatabaseConfig, broker: Broker) -> None:
    inserted = [("evt_z", "ws_a1"), ("evt_y", "ws_b1"), ("evt_x", "ws_a1"), ("evt_w", "ws_a1"), ("evt_v", "ws_b1")]

    async def scenario() -> None:
        connection = await nats.connect(broker.url, max_reconnect_attempts=-1, reconnect_time_wait=0.1)
        try:
            async with _store(config) as store:
                worker = OutboxPublisher(
                    store, JetStreamEvents(connection.jetstream(), timeout=0.5),
                    idle_interval=0.05, min_backoff=0.05, max_backoff=0.2,
                )
                worker.start()
                try:
                    await asyncio.to_thread(broker.stop)
                    for event_id, workspace_id in inserted:
                        async with store.transaction() as tx:
                            await tx.append_event(_event(event_id, workspace_id))
                        worker.notify()
                    await asyncio.sleep(1.5)
                    assert [event.event_id for event in await store.pending_events(10)] == [e for e, _ in inserted]
                    await asyncio.to_thread(broker.start)
                    await _until_async(lambda: store.pending_events(10), [], timeout=20)
                finally:
                    await worker.close()
        finally:
            await connection.close()
        published = await _stream(broker.url)
        # Each event exactly once; ordering is promised only within a workspace.
        assert sorted(event_id for _, event_id, _ in published) == sorted(e for e, _ in inserted)
        for workspace in ("ws_a1", "ws_b1"):
            ids = [envelope["event_id"] for _, _, envelope in published if envelope["resources"]["workspace_id"] == workspace]
            assert ids == [e for e, w in inserted if w == workspace]

    asyncio.run(scenario())


@pytest.mark.integration
def test_service_restart_publishes_events_committed_before_a_crash(tmp_path: Path, broker: Broker) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path / "db")
    environment = {
        **os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "OE_NATS_URL": broker.url,
        "OE_DATABASE_BACKEND": "sqlite", "OE_SQLITE_DIRECTORY": str(config.sqlite_directory),
        "OE_VCP_STATE_DIR": str(tmp_path / "vcp"), "OE_VCP_HEALTH_PORT": "0",
        "OE_VCP_READY_FILE": str(tmp_path / "ready"),
    }
    PERSISTENCE.upgrade(config)

    async def commit() -> None:
        # The previous process committed, then died before publishing.
        async with _store(config) as store:
            await accept(store, _command(), "evt_1")

    asyncio.run(commit())
    with (tmp_path / "vcp.log").open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "virtual_control_plane"], env=environment,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            async def published() -> None:
                async with _store(config) as store:
                    await _until_async(lambda: store.pending_events(10), [], timeout=20)

            asyncio.run(published())
            process.terminate()
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert asyncio.run(_stream(broker.url)) == [
        ("oe.v1.vcp.events.workspaces.ws_a1.workspace.provisioning", "evt_1", _event("evt_1")),
    ]
