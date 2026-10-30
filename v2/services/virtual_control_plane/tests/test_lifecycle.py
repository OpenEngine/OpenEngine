"""Public requests over a real broker, durable effects and recovery using fake compute."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import nats
import pytest
from nats.errors import TimeoutError as NatsTimeoutError
from openengine.messaging import Client, LocalOperator, Server, ServiceError, envelopes, listen_address
from openengine.persistence import DatabaseConfig
from openengine.supervisor.processes.nats import install_nats_server

from virtual_control_plane.application.lifecycle import Lifecycle, SCHEMAS
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.tests.fakes import FakeAllocationProvider as FakeProvider

APPLY = "oe.v1.vcp.control.workspaces.apply"
QUERY = "oe.v1.vcp.query."
DOCUMENT = {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": "example"},
            "spec": {"provider": "process"}}


@pytest.fixture(scope="module")
def binary():
    return install_nats_server(Path(tempfile.gettempdir()) / "oe-test-nats")


@asynccontextmanager
async def broker(binary, host="127.0.0.1"):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([str(binary), "-a", host, "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    connection = None
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.02)
        connection = await nats.connect(f"nats://127.0.0.1:{port}")
        yield connection
    finally:
        if connection:
            await connection.close()
        process.terminate()
        process.wait(timeout=5)


def storage(tmp_path):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return DurableStore(PERSISTENCE.database(config))


@asynccontextmanager
async def serving(connection, store, provider):
    lifecycle = Lifecycle(store, {"process": provider})
    server = Server(connection, "vcp", listen_address(connection), queries=lifecycle.queries(), commands=lifecycle.commands())
    await server.start()
    try:
        yield lifecycle, server, Client(connection)
    finally:
        await server.drain()


class DropReply:
    def __init__(self, connection):
        self.connection, self.dropped = connection, False
        self.response = None

    async def request(self, *args, **kwargs):
        response = await self.connection.request(*args, **kwargs)
        if not self.dropped:
            self.dropped = True
            self.response = json.loads(response.data)
            raise NatsTimeoutError()
        return response


def test_nats_retry_concurrency_restart_and_typed_queries(binary, tmp_path, monkeypatch):
    from virtual_control_plane.persistence.durable import Transaction
    original_all = Transaction.all

    async def no_workspace_scan(self, collection):
        assert collection != "workspaces"
        return await original_all(self, collection)

    monkeypatch.setattr(Transaction, "all", no_workspace_scan)
    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        async with broker(binary) as connection:
            async with serving(connection, store, provider) as (lifecycle, server, client):
                dropped = DropReply(connection)
                response = await Client(dropped).command(APPLY, {"workspace": DOCUMENT}, command_id="cmd_same")
                assert response == dropped.response
                concurrent = await asyncio.gather(*(client.command(APPLY, {"workspace": DOCUMENT}, command_id="cmd_same") for _ in range(8)))
                assert concurrent == [response] * 8
                await asyncio.gather(lifecycle.reconcile(), lifecycle.reconcile())
                assert provider.calls == len(provider.allocations) == 1
                workspace = (await client.query(QUERY + f"workspaces.{response['resource_id']}.get"))["result"]["workspace"]
                envelopes.check(f"{SCHEMAS}/workspace.schema.json#/$defs/workspace", workspace)
                assert workspace["state"] == "ready"
                operation = (await client.query(QUERY + f"operations.{response['operation_id']}.get"))["result"]["operation"]
                envelopes.check(f"{SCHEMAS}/operations.schema.json#/$defs/operation", operation)
                assert operation["status"] == "succeeded"
                for subject, code in [(QUERY + "workspaces.bad.get", "INVALID_ARGUMENT"),
                                      (QUERY + "workspaces.ws_missing.get", "NOT_FOUND"),
                                      ("oe.v1.vcp.control.workspaces.ws_missing.ensure", "NOT_FOUND"),
                                      ("oe.v1.vcp.control.workspaces.ws_missing.destroy", "UNSUPPORTED_CAPABILITY")]:
                    reply = json.loads((await connection.request(subject, envelopes.encode({"params": {}} if ".query." in subject else envelopes.command({})))).data)
                    assert reply["error"]["code"] == code
                lost = DropReply(connection)
                third = deepcopy(DOCUMENT)
                third["metadata"]["name"] = "lost-reply"
                with pytest.raises(ServiceError, match="UNAVAILABLE"):
                    await Client(lost, attempts=1).command(APPLY, {"workspace": third}, command_id="cmd_lost")
                assert lost.response["status"] == "accepted"
                bad = {"command_id": "cmd_bad_schema", "payload": {}, "causal_path": ["cmd_bad_schema"]}
                rejected = json.loads((await connection.request(APPLY, envelopes.encode(bad))).data)
                assert rejected["error"]["code"] == "INVALID_ARGUMENT"
                assert json.loads((await connection.request(APPLY, envelopes.encode(bad))).data) == rejected
                with pytest.raises(ServiceError, match="COMMAND_ID_CONFLICT"):
                    await client.command(APPLY, {"workspace": DOCUMENT}, command_id="cmd_bad_schema")
                with pytest.raises(ServiceError, match="VERSION_CONFLICT"):
                    await client.command(APPLY, {"workspace": DOCUMENT}, expected_version=1)
                second = deepcopy(DOCUMENT)
                second["metadata"]["name"] = "second"
                await client.command(APPLY, {"workspace": second}, command_id="cmd_second")
                page = (await client.query(QUERY + "workspaces.list", {"limit": 1.0}))["result"]
                assert page["next_cursor"]
            await store.close()
            store = storage(tmp_path)
            async with serving(connection, store, provider) as (lifecycle, server, client):
                assert await client.command(APPLY, {"workspace": third}, command_id="cmd_lost") == lost.response
                assert await client.command(APPLY, {"workspace": DOCUMENT}, command_id="cmd_same", deadline_at="2000-01-01T00:00:00Z") == response
                next_page = (await client.query(QUERY + "workspaces.list", {"limit": 1, "cursor": page["next_cursor"]}))["result"]
                assert next_page["items"][0]["workspace_id"] != page["items"][0]["workspace_id"]
                with pytest.raises(ServiceError, match="INVALID_CURSOR"):
                    await client.query(QUERY + "workspaces.list", {"provider": "process", "cursor": page["next_cursor"]})
                await lifecycle.reconcile()
                assert len(provider.allocations) == 3
            await store.close()
    asyncio.run(scenario())


def test_denied_requests_cannot_replay_reserve_or_read(binary, tmp_path):
    class Policy(LocalOperator):
        denied = False
        def authorize(self, request):
            if self.denied:
                raise ServiceError("PERMISSION_DENIED", "denied")
            super().authorize(request)

    async def scenario():
        store, provider, policy = storage(tmp_path), FakeProvider(), Policy()
        async with broker(binary) as connection:
            async with serving(connection, store, provider) as (_, server, client):
                server.authorizer = policy
                accepted = await client.command(APPLY, {"workspace": DOCUMENT}, command_id="cmd_existing")
                before = await store.pending_events(100)
                policy.denied = True
                for command_id in ("cmd_existing", "cmd_denied"):
                    with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                        await client.command(APPLY, {"workspace": DOCUMENT}, command_id=command_id)
                for route in ("capabilities.get", "workspaces.list", f"workspaces.{accepted['resource_id']}.get", f"operations.{accepted['operation_id']}.get"):
                    with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                        await client.query(QUERY + route)
                async with store.transaction() as tx:
                    assert await tx.replay(APPLY, envelopes.command({"workspace": DOCUMENT}, command_id="cmd_denied")) is None
                    assert len(await tx.all("workspaces")) == len(await tx.all("operations")) == 1
                assert await store.pending_events(100) == before
                assert provider.calls == 0
        await store.close()
    asyncio.run(scenario())


def test_loopback_dial_to_public_broker_is_denied(binary, tmp_path):
    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        async with broker(binary, "0.0.0.0") as connection:
            async with serving(connection, store, provider) as (_, _, client):
                with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                    await client.command(APPLY, {"workspace": DOCUMENT})
                with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                    await client.query(QUERY + "workspaces.list")
                async with store.transaction() as tx:
                    assert await tx.all("workspaces") == []
                assert await store.pending_events(100) == []
                assert provider.calls == 0
        await store.close()
    asyncio.run(scenario())


def test_crash_after_allocation_recovers_same_runtime_and_failed_allocation_retains_ownership(tmp_path, monkeypatch):
    from virtual_control_plane.application import lifecycle as module

    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        lifecycle = Lifecycle(store, {"process": provider})
        body = envelopes.command({"workspace": DOCUMENT}, command_id="cmd_crash")
        from openengine.messaging import Request
        request = Request(APPLY, "control", "workspaces.apply", body, "nats://127.0.0.1:4222")
        response = await lifecycle.apply(request)
        provider.crash = True
        await lifecycle.reconcile()
        await store.close()
        store = storage(tmp_path)
        lifecycle = Lifecycle(store, {"process": provider})
        assert await lifecycle.apply(request) == response
        await lifecycle.reconcile()
        assert provider.calls == 2 and len(provider.allocations) == 1
        document = deepcopy(DOCUMENT)
        document["metadata"]["name"] = "failed"
        body = envelopes.command({"workspace": document})
        response = await lifecycle.apply(Request(APPLY, "control", "workspaces.apply", body, request.listener))
        provider.failure = ServiceError("UNAVAILABLE", "temporary")
        await lifecycle.reconcile()
        async with store.transaction() as tx:
            record = await tx.get("operations", response["operation_id"])
            # Claimed and in backoff: still unfinished, so a restart resumes it.
            assert record["snapshot"]["status"] == "running"
            assert record["attempts"] == 1 and record["not_before"] > module.now()
        calls = provider.calls
        await lifecycle.reconcile()  # Not due yet: the provider is left alone.
        assert provider.calls == calls
        monkeypatch.setattr(module, "RETRY_BASE_SECONDS", 0)
        async with store.transaction() as tx:
            record["not_before"] = module.now()
            await tx.put("operations", response["operation_id"], record)
        provider.failure = ServiceError("PROVIDER_ERROR", "allocation failed")
        await lifecycle.reconcile()
        async with store.transaction() as tx:
            record = await tx.get("operations", response["operation_id"])
            assert record["snapshot"]["status"] == "failed"
            assert record["snapshot"]["error"]["code"] == "PROVIDER_ERROR"
            assert "attempts" not in record and "not_before" not in record
            assert await tx.get("workspace_runtimes", response["resource_id"])
            assert "active_operation_id" not in await tx.get("workspaces", response["resource_id"])
        await store.close()
    asyncio.run(scenario())


def test_durable_rejections_and_atomic_rollback(tmp_path):
    from openengine.messaging import Request

    async def scenario():
        store = storage(tmp_path)
        lifecycle = Lifecycle(store, {"process": FakeProvider()})

        async def send(body):
            return await lifecycle.apply(Request(APPLY, "control", "workspaces.apply", body, "nats://127.0.0.1:4222"))

        for body, code in [
            ({"command_id": "cmd_malformed"}, "INVALID_ARGUMENT"),
            (envelopes.command({"workspace": DOCUMENT}, command_id="cmd_expired", deadline_at="2000-01-01T00:00:00Z"), "DEADLINE_EXCEEDED"),
            (envelopes.command({"workspace": DOCUMENT}, command_id="cmd_version", expected_version=1), "NOT_FOUND"),
            (envelopes.command({"workspace": DOCUMENT}, command_id="cmd_path", caused_by=[f"root_{i}" for i in range(31)]), "CAUSAL_PATH_TOO_LONG"),
        ]:
            response = await send(body)
            assert response["error"]["code"] == code
            envelopes.check(envelopes.COMMAND_RESPONSE, response)
            assert await send(body) == response
        with pytest.raises(ServiceError, match="COMMAND_ID_CONFLICT"):
            await send(envelopes.command({"workspace": DOCUMENT}, command_id="cmd_malformed"))
        async with store.transaction() as tx:
            assert await tx.all("workspaces") == await tx.all("operations") == []
        assert await store.pending_events(100) == []

        class FailingStore:
            @asynccontextmanager
            async def transaction(self):
                async with store.transaction() as tx:
                    append = tx.append_event
                    async def fail(event):
                        await append(event)
                        raise RuntimeError("failure after event insertion")  # noqa: TRY003 -- injected transaction fault
                    tx.append_event = fail
                    yield tx

        body = envelopes.command({"workspace": DOCUMENT}, command_id="cmd_rollback")
        failing = Lifecycle(FailingStore(), lifecycle.providers)
        with pytest.raises(RuntimeError, match="after event"):
            await failing.apply(Request(APPLY, "control", "workspaces.apply", body, "nats://127.0.0.1:4222"))
        async with store.transaction() as tx:
            assert await tx.replay(APPLY, body) is None
            for collection in ("workspaces", "operations", "workspace_runtimes"):
                assert await tx.all(collection) == []
        assert await store.pending_events(100) == []
        assert (await send(body))["status"] == "accepted"
        await store.close()
    asyncio.run(scenario())


def test_allocation_pages_are_indexed_bounded_concurrent_and_claimed(tmp_path):
    from openengine.messaging import Request
    from virtual_control_plane.persistence.durable import Transaction

    class GatedProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.peak = 0
            self.full = asyncio.Event()
            self.release = asyncio.Event()

        async def provision(self, runtime_id, workspace):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 8:
                self.full.set()
            try:
                await self.release.wait()
                return await super().provision(runtime_id, workspace)
            finally:
                self.active -= 1

    async def scenario():
        store, provider = storage(tmp_path), GatedProvider()
        lifecycle = Lifecycle(store, {"process": provider})
        for i in range(19):
            document = deepcopy(DOCUMENT)
            document["metadata"]["name"] = f"workspace-{i}"
            await lifecycle.apply(Request(
                APPLY, "control", "workspaces.apply",
                envelopes.command({"workspace": document}), "nats://127.0.0.1:4222",
            ))
        # Terminal history is excluded by the partial index, not decoded by Python.
        async with store.transaction() as tx:
            for i in range(200):
                await tx.put("operations", f"history_{i}", {"snapshot": {"status": "succeeded"}})
            plan = await tx._connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT body FROM operations "
                "WHERE json_extract(body, '$.snapshot.status') IN ('pending', 'running') "
                "AND key > '' ORDER BY key LIMIT 8"
            )
            assert "ix_operations_active" in str(plan.all())
        original = Transaction.all

        async def no_scan(self, collection):
            assert collection != "operations"
            return await original(self, collection)

        Transaction.all = no_scan
        try:
            first = asyncio.create_task(lifecycle.reconcile())
            second = asyncio.create_task(lifecycle.reconcile())
            await asyncio.wait_for(provider.full.wait(), 5)
            assert provider.active == provider.peak == 8
            provider.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 10)
            assert provider.calls == len(provider.allocations) == 19
            assert provider.peak == 8
            await lifecycle.reconcile()  # Empty indexed lookup with terminal history.
            assert provider.calls == 19
        finally:
            Transaction.all = original
            await store.close()

    asyncio.run(scenario())


def test_stalled_allocation_refills_slots_and_times_out_pending(tmp_path, monkeypatch):
    from openengine.messaging import Request
    from virtual_control_plane.application import lifecycle as module

    class StalledProvider(FakeProvider):
        async def provision(self, runtime_id, workspace):
            if workspace["metadata"]["name"] == "stall":
                await asyncio.Event().wait()
            return await super().provision(runtime_id, workspace)

    async def scenario():
        store, provider = storage(tmp_path), StalledProvider()
        lifecycle = Lifecycle(store, {"process": provider})
        monkeypatch.setattr(module, "ALLOCATION_TIMEOUT_SECONDS", 2)
        ids = []
        for i in range(10):
            document = deepcopy(DOCUMENT)
            document["metadata"]["name"] = "stall" if i == 0 else f"fast-{i}"
            response = await lifecycle.apply(Request(APPLY, "control", "workspaces.apply",
                envelopes.command({"workspace": document}), "nats://127.0.0.1:4222"))
            ids.append(response["operation_id"])
        # Force the stalled operation into the first page regardless of UUID order.
        async with store.transaction() as tx:
            record = await tx.get("operations", ids[0])
            await tx._connection.exec_driver_sql("DELETE FROM operations WHERE key = ?", (ids[0],))
            record["snapshot"]["operation_id"] = "op_000"
            await tx.put("operations", "op_000", record)
        task = asyncio.create_task(lifecycle.reconcile())
        async with asyncio.timeout(1):
            while provider.calls < 9:
                await asyncio.sleep(0.01)
        assert not task.done()  # Ninth fast allocation ran while the first was stalled.
        await asyncio.wait_for(task, 3)
        async with store.transaction() as tx:
            stalled = await tx.get("operations", "op_000")
            # Timed out: still claimed and unfinished, retried with backoff rather than failed.
            assert stalled["snapshot"]["status"] == "running" and stalled["attempts"] == 1
        await store.close()

    asyncio.run(scenario())


def test_workspace_queries_use_indexes_and_do_not_reserve_writer(tmp_path):
    async def scenario():
        store = storage(tmp_path)
        async with store.transaction() as tx:
            for i in range(300):
                await tx.put("workspaces", f"ws_{i:04}", {"snapshot": {
                    "workspace_id": f"ws_{i:04}", "name": f"name-{i}",
                    "created_at": "2026-01-01T00:00:00Z", "state": "ready",
                    "provider": "process", "labels": {"job_id": "job_test"}}})
            plan = await tx._connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT body FROM workspaces "
                "WHERE json_extract(body, '$.snapshot.name') = 'name-299' LIMIT 1")
            assert "ix_workspaces_name" in str(plan.all())
            assert (await tx.workspace_named("name-299"))["snapshot"]["workspace_id"] == "ws_0299"
            plan = await tx._connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT body FROM workspaces "
                "WHERE json_type(body, '$.snapshot') = 'object' "
                "AND (json_extract(body, '$.snapshot.created_at'), key) < ('2026-01-01T00:00:00Z', 'ws_0299') "
                "ORDER BY json_extract(body, '$.snapshot.created_at') DESC, key DESC LIMIT 2")
            assert "ix_workspaces_page" in str(plan.all())
        # An independently held writer reservation cannot block this read.
        async with store.transaction():
            page = await asyncio.wait_for(store.workspace_page({}, None, 2), 1)
        assert [w["workspace_id"] for w in page] == ["ws_0299", "ws_0298"]
        page = await store.workspace_page({"state": ["ready"], "provider": "process", "job_id": "job_test"},
                                        [page[-1]["created_at"], page[-1]["workspace_id"]], 2)
        assert [w["workspace_id"] for w in page] == ["ws_0297", "ws_0296"]
        await store.close()

    asyncio.run(scenario())


def test_provider_cancellation_keeps_background_worker_alive(tmp_path):
    from openengine.messaging import Request

    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        lifecycle = Lifecycle(store, {"process": provider})
        provider.crash = True
        first = await lifecycle.apply(Request(APPLY, "control", "workspaces.apply",
            envelopes.command({"workspace": DOCUMENT}), "nats://127.0.0.1:4222"))
        lifecycle.start()
        try:
            async with asyncio.timeout(5):
                while not provider.calls:
                    await asyncio.sleep(0.01)
            # Claimed before the provider was interrupted; the next pass resumes it.
            assert (await store.read("operations", first["operation_id"]))["snapshot"]["status"] == "running"
            document = deepcopy(DOCUMENT)
            document["metadata"]["name"] = "later"
            second = await lifecycle.apply(Request(APPLY, "control", "workspaces.apply",
                envelopes.command({"workspace": document}), "nats://127.0.0.1:4222"))
            async with asyncio.timeout(5):
                while (await store.read("operations", second["operation_id"]))["snapshot"]["status"] != "succeeded":
                    await asyncio.sleep(0.01)
            assert not lifecycle._worker.done()
            assert len(provider.allocations) == 2
        finally:
            await asyncio.wait_for(lifecycle.close(), 1)
            assert lifecycle._worker.cancelled()
            await store.close()

    asyncio.run(scenario())


def test_status_queries_do_not_reserve_writer(tmp_path):
    from openengine.messaging import Request

    async def scenario():
        store = storage(tmp_path)
        lifecycle = Lifecycle(store, {"process": FakeProvider()})
        response = await lifecycle.apply(Request(APPLY, "control", "workspaces.apply",
            envelopes.command({"workspace": DOCUMENT}), "nats://127.0.0.1:4222"))
        async with store.transaction():
            for collection, key in (("workspaces", response["resource_id"]),
                                    ("operations", response["operation_id"])):
                snapshot = await asyncio.wait_for(lifecycle._read(collection, key), 1)
                assert snapshot
            with pytest.raises(ServiceError, match="NOT_FOUND"):
                await asyncio.wait_for(lifecycle._read("workspaces", "ws_missing"), 1)
        await store.close()

    asyncio.run(scenario())


def _request(subject: str, body, workspace_id: str | None = None):
    from openengine.messaging import Request
    operation = subject.removeprefix("oe.v1.vcp.control.")
    parameters = {"workspace_id": workspace_id} if workspace_id else {}
    return Request(subject, "control", operation, body, "nats://127.0.0.1:4222", parameters)


def _ensure(workspace_id: str, payload=None, **extra):
    return _request(f"oe.v1.vcp.control.workspaces.{workspace_id}.ensure",
                    envelopes.command(payload or {}, **extra), workspace_id)


async def _snapshot(store, collection, key):
    return (await store.read(collection, key))["snapshot"]


def test_ensure_is_idempotent_and_recovers_pending_and_running_operations(tmp_path):
    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        lifecycle = Lifecycle(store, {"process": provider})
        created = await lifecycle.apply(_request(APPLY, envelopes.command({"workspace": DOCUMENT})))
        workspace_id = created["resource_id"]
        await lifecycle.reconcile()
        ready = await _snapshot(store, "workspaces", workspace_id)
        assert ready["state"] == "ready" and ready["version"] == 2
        events = len(await store.pending_events(100))

        missing = await lifecycle.ensure(_ensure("ws_missing", command_id="cmd_missing"))
        assert missing["error"]["code"] == "NOT_FOUND"
        assert await lifecycle.ensure(_ensure("ws_missing", command_id="cmd_missing")) == missing
        stale = await lifecycle.ensure(_ensure(workspace_id, expected_version=1))
        assert stale["error"]["code"] == "VERSION_CONFLICT"
        for response in (missing, stale):
            envelopes.check(envelopes.COMMAND_RESPONSE, response)

        accepted = await lifecycle.ensure(_ensure(workspace_id, {"restore": "allow"}, command_id="cmd_ensure"))
        assert accepted["status"] == "accepted" and accepted["resource_id"] == workspace_id
        busy = await lifecycle.ensure(_ensure(workspace_id, command_id="cmd_busy"))
        assert busy["error"]["code"] == "UNAVAILABLE" and busy["error"]["retryable"]
        async with store.transaction() as tx:
            assert await tx.replay(f"oe.v1.vcp.control.workspaces.{workspace_id}.ensure", busy) is None
            assert (await tx.get("workspaces", workspace_id))["active_operation_id"] == accepted["operation_id"]
        await lifecycle.reconcile()
        operation = await _snapshot(store, "operations", accepted["operation_id"])
        envelopes.check(f"{SCHEMAS}/operations.schema.json#/$defs/operation", operation)
        assert operation["status"] == "succeeded"
        assert operation["result"] == {"runtime_id": ready["runtime"]["runtime_id"], "runtime_generation": 1, "restored": False}
        # A no-op ensure: same version, same timestamps, no second ready event, lock released.
        assert await _snapshot(store, "workspaces", workspace_id) == ready
        assert len(await store.pending_events(100)) == events
        assert "active_operation_id" not in await store.read("workspaces", workspace_id)
        assert provider.ensures == 1 and provider.calls == 1
        assert await lifecycle.ensure(_ensure(workspace_id, {"restore": "allow"}, command_id="cmd_ensure")) == accepted
        # The lock is free again, so a retried busy command is accepted now.
        assert (await lifecycle.ensure(_ensure(workspace_id, command_id="cmd_busy")))["status"] == "accepted"
        await lifecycle.reconcile()

        # Concurrent state written by another producer while the provider runs survives.
        class Interleaving(FakeProvider):
            async def ensure(self, ownership, locator, specification):
                async with store.transaction() as tx:
                    document = await tx.get("workspaces", workspace_id)
                    document["snapshot"]["active_attempt_ids"] = ["attempt_live"]
                    await tx.put("workspaces", workspace_id, document)
                return await super().ensure(ownership, locator, specification)

        interleaving = Interleaving(allocations=provider.allocations)
        lifecycle = Lifecycle(store, {"process": interleaving})
        await lifecycle.ensure(_ensure(workspace_id, command_id="cmd_interleaved"))
        await lifecycle.reconcile()
        assert (await _snapshot(store, "workspaces", workspace_id))["active_attempt_ids"] == ["attempt_live"]

        # Pending and running ensures survive a restart with the same ownership.
        pending = await lifecycle.ensure(_ensure(workspace_id, command_id="cmd_pending"))
        await store.close()
        store = storage(tmp_path)
        lifecycle = Lifecycle(store, {"process": FakeProvider(allocations=provider.allocations)})
        await lifecycle.reconcile()
        assert (await _snapshot(store, "operations", pending["operation_id"]))["status"] == "succeeded"
        running = await lifecycle.ensure(_ensure(workspace_id, command_id="cmd_running"))
        async with store.transaction() as tx:
            record = await tx.get("operations", running["operation_id"])
            record["snapshot"]["status"] = "running"  # claimed, then the process died
            await tx.put("operations", running["operation_id"], record)
        await store.close()
        store = storage(tmp_path)
        lifecycle = Lifecycle(store, {"process": FakeProvider(allocations=provider.allocations)})
        await lifecycle.reconcile()
        assert (await _snapshot(store, "operations", running["operation_id"]))["status"] == "succeeded"
        assert (await _snapshot(store, "workspaces", workspace_id))["version"] == 2

        # A failed ensure fails its operation only: the workspace and ownership stay.
        failing = FakeProvider(allocations=provider.allocations)
        failing.failure = ServiceError("PROVIDER_ERROR", "guest is unreachable")
        lifecycle = Lifecycle(store, {"process": failing})
        failed = await lifecycle.ensure(_ensure(workspace_id, command_id="cmd_failing"))
        await lifecycle.reconcile()
        operation = await _snapshot(store, "operations", failed["operation_id"])
        assert operation["status"] == "failed" and operation["error"]["code"] == "PROVIDER_ERROR"
        after = await _snapshot(store, "workspaces", workspace_id)
        assert after["state"] == "ready" and after["version"] == 2 and "failure" not in after
        assert len(await store.pending_events(100)) == events
        assert await store.read("workspace_runtimes", workspace_id)

        # Only a ready workspace can be ensured.
        broken = deepcopy(DOCUMENT)
        broken["metadata"]["name"] = "broken"
        failing.failure = ServiceError("PROVIDER_ERROR", "no image")
        accepted = await lifecycle.apply(_request(APPLY, envelopes.command({"workspace": broken})))
        await lifecycle.reconcile()
        rejected = await lifecycle.ensure(_ensure(accepted["resource_id"]))
        assert rejected["error"]["code"] == "INVALID_STATE"
        assert rejected["error"]["details"]["current_state"] == "failed"
        envelopes.check(envelopes.COMMAND_RESPONSE, rejected)
        unconfigured = await Lifecycle(store, {}).ensure(_ensure(workspace_id))
        assert unconfigured["error"]["code"] == "UNSUPPORTED_CAPABILITY"
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["pending", "running"])
@pytest.mark.parametrize("operation", ["workspaces.apply", "workspaces.ensure"])
def test_missing_provider_fails_operations_explicitly_without_stalling_other_work(tmp_path, operation, status):
    async def scenario():
        store = storage(tmp_path)
        process, other = FakeProvider(), FakeProvider("other")
        lifecycle = Lifecycle(store, {"process": process, "other": other})
        created = await lifecycle.apply(_request(APPLY, envelopes.command({"workspace": DOCUMENT})))
        workspace_id, response = created["resource_id"], created
        if operation == "workspaces.ensure":
            await lifecycle.reconcile()
            response = await lifecycle.ensure(_ensure(workspace_id))
        async with store.transaction() as tx:
            record = await tx.get("operations", response["operation_id"])
            record["snapshot"]["status"] = status
            await tx.put("operations", response["operation_id"], record)
            ownership = await tx.get("workspace_runtimes", workspace_id)
        before = await _snapshot(store, "workspaces", workspace_id)
        events = len(await store.pending_events(100))
        unrelated_document = deepcopy(DOCUMENT)
        unrelated_document["metadata"]["name"] = "unrelated"
        unrelated_document["spec"]["provider"] = "other"
        unrelated = await lifecycle.apply(_request(APPLY, envelopes.command({"workspace": unrelated_document})))
        await store.close()

        # Restart with only the unrelated provider configured.
        store = storage(tmp_path)
        lifecycle = Lifecycle(store, {"other": other})
        await lifecycle.reconcile()
        failed = await _snapshot(store, "operations", response["operation_id"])
        envelopes.check(f"{SCHEMAS}/operations.schema.json#/$defs/operation", failed)
        assert failed["status"] == "failed"
        assert failed["error"] == {"code": "PROVIDER_ERROR", "retryable": False, "details": {"provider": "process"},
                                   "message": "provider process is not configured on this host"}
        workspace = await store.read("workspaces", workspace_id)
        assert "active_operation_id" not in workspace  # the lock is released
        if operation == "workspaces.apply":
            assert workspace["snapshot"]["state"] == "failed" and workspace["snapshot"]["version"] == before["version"] + 1
            # The unrelated workspace's provisioning and ready events, plus this failure.
            assert len(await store.pending_events(100)) == events + 3
        else:
            assert workspace["snapshot"] == before
            assert len(await store.pending_events(100)) == events + 2  # the unrelated workspace's events only
        assert await store.read("workspace_runtimes", workspace_id) == ownership  # retained for cleanup
        assert (await _snapshot(store, "operations", unrelated["operation_id"]))["status"] == "succeeded"
        assert process.calls + process.ensures == (1 if operation == "workspaces.ensure" else 0)

        # Once the provider is back, a ready workspace can be ensured with a new command.
        if operation == "workspaces.ensure":
            lifecycle = Lifecycle(store, {"process": process, "other": other})
            recovered = await lifecycle.ensure(_ensure(workspace_id))
            assert recovered["status"] == "accepted"
            await lifecycle.reconcile()
            assert (await _snapshot(store, "operations", recovered["operation_id"]))["status"] == "succeeded"
        await store.close()

    asyncio.run(scenario())


def test_transient_failures_back_off_with_a_cap_and_eventually_fail(tmp_path, monkeypatch):
    from virtual_control_plane.application import lifecycle as module

    assert [module.retry_delay(n) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]
    monkeypatch.setattr(module, "RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(module, "MAX_ATTEMPTS", 3)

    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        provider.failure = ServiceError("UNAVAILABLE", "host is busy")
        lifecycle = Lifecycle(store, {"process": provider})
        created = await lifecycle.apply(_request(APPLY, envelopes.command({"workspace": DOCUMENT})))
        for attempt in (1, 2):
            await lifecycle.reconcile()
            record = await store.read("operations", created["operation_id"])
            assert record["snapshot"]["status"] == "running" and record["attempts"] == attempt
            assert record["not_before"] <= module.now()
        await lifecycle.reconcile()
        record = await store.read("operations", created["operation_id"])
        assert record["snapshot"]["status"] == "failed" and "attempts" not in record
        error = record["snapshot"]["error"]
        assert error["code"] == "PROVIDER_ERROR" and not error["retryable"]
        assert error["details"] == {"attempts": 3, "last_error": provider.failure.to_dict()}
        envelopes.check(f"{SCHEMAS}/operations.schema.json#/$defs/operation", record["snapshot"])
        workspace = await _snapshot(store, "workspaces", created["resource_id"])
        assert workspace["state"] == "failed" and workspace["failure"] == error
        assert [event.envelope["event_type"] for event in await store.pending_events(100)] == \
            ["workspace.provisioning", "workspace.failed"]
        assert provider.calls == 3
        await store.close()

    asyncio.run(scenario())
