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

APPLY = "oe.v1.vcp.control.workspaces.apply"
QUERY = "oe.v1.vcp.query."
DOCUMENT = {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": "example"},
            "spec": {"provider": "process"}}


class FakeProvider:
    capabilities = {"provider": "process", "isolation": "process", "checkpoint": False, "suspend": False,
                    "restore": "none", "approvals_survive_restore": False, "max_resources": {}, "tools": []}

    def __init__(self):
        self.allocations = {}
        self.calls = 0
        self.failure = None
        self.crash = False

    async def provision(self, runtime_id, workspace):
        self.calls += 1
        if self.failure:
            raise self.failure
        self.allocations.setdefault(runtime_id, {"fake": runtime_id})
        if self.crash:
            self.crash = False
            raise asyncio.CancelledError()
        return self.allocations[runtime_id]


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
                                      ("oe.v1.vcp.control.workspaces.ws_missing.ensure", "UNSUPPORTED_CAPABILITY")]:
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


def test_crash_after_allocation_recovers_same_runtime_and_failed_allocation_retains_ownership(tmp_path):
    async def scenario():
        store, provider = storage(tmp_path), FakeProvider()
        lifecycle = Lifecycle(store, {"process": provider})
        body = envelopes.command({"workspace": DOCUMENT}, command_id="cmd_crash")
        from openengine.messaging import Request
        request = Request(APPLY, "control", "workspaces.apply", body, "nats://127.0.0.1:4222")
        response = await lifecycle.apply(request)
        provider.crash = True
        with pytest.raises(asyncio.CancelledError):
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
            assert (await tx.get("operations", response["operation_id"]))["snapshot"]["status"] == "pending"
        provider.failure = ServiceError("PROVIDER_ERROR", "allocation failed")
        await lifecycle.reconcile()
        async with store.transaction() as tx:
            assert (await tx.get("operations", response["operation_id"]))["snapshot"]["status"] == "failed"
            assert await tx.get("workspace_runtimes", response["resource_id"])
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
                "WHERE json_extract(body, '$.snapshot.status') = 'pending' "
                "AND key > '' ORDER BY key LIMIT 8"
            )
            assert "ix_operations_pending" in str(plan.all())
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
            assert (await tx.get("operations", "op_000"))["snapshot"]["status"] == "pending"
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
