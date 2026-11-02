"""Deterministic acceptance evidence; no daemon, broker delivery or model execution."""
import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest
import sqlalchemy as sa

from openengine.contracts.runtime import validate_message
from openengine.messaging import Request, ServiceError, envelopes
from openengine.messaging.errors import invalid_argument
from openengine.persistence import DatabaseConfig

from virtual_control_plane.application.execution import Execution, InvalidTransition
from virtual_control_plane.application.lifecycle import Lifecycle, SCHEMAS
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore, Transaction
from virtual_control_plane.persistence.schema import runtime_commands


class Context:
    def __init__(self, root):
        self.root = root
        self.value = {"working_directory": ".", "instruction_files": ["AGENTS.md"], "references": []}

    async def prepare(self, workspace, runtime, payload):
        assert workspace["specification"]["apiVersion"] == "openengine.cc/v1alpha1"
        if payload["agent"].get("settings"):
            raise invalid_argument([("agent.settings", "unsupported fixture configuration")])
        return str(self.root), deepcopy(self.value)


def request(command_id="cmd_execute", payload=None, operation="execute", **body):
    payload = payload if payload is not None else {"agent": {"runner": "fixture"}, "task": {"instruction": "Test"}, "session": {"mode": "new"}}
    return Request(f"oe.v1.vcp.control.workspaces.ws_test.{operation}", "control", "workspaces." + operation,
                   envelopes.command(payload, command_id=command_id, **body), "nats://127.0.0.1:4222", {"workspace_id": "ws_test"})


def query(**params):
    return Request("oe.v1.vcp.query.workspaces.ws_test.sessions.list", "query", "workspaces.sessions.list",
                   {"params": params}, "nats://127.0.0.1:4222", {"workspace_id": "ws_test", "session_id": "session_missing"})


async def setup(tmp_path):
    config = DatabaseConfig(sqlite_directory=tmp_path / "db")
    PERSISTENCE.upgrade(config)
    store = DurableStore(PERSISTENCE.database(config))
    root = tmp_path / "root"
    root.mkdir()
    (root / "AGENTS.md").write_text("Test instructions")
    async with store.transaction() as tx:
        await tx.put("workspaces", "ws_test", {"specification": {"apiVersion": "openengine.cc/v1alpha1"}, "snapshot": {
            "workspace_id": "ws_test", "state": "ready", "version": 2, "active_attempt_ids": [],
            "runtime": {"runtime_id": "rt_test", "generation": 1, "state": "running"}}})
        await tx.put("workspace_runtimes", "ws_test", {"runtime_id": "rt_test", "generation": 1, "locator": {"path": str(root)}})
        await tx.put("runtime_status", "rt_test", {"workspace_id": "ws_test", "generation": 1, "ready": True,
                                                  "executors": ["fixture"], "max_concurrent_attempts": 8})
    lifecycle = Lifecycle(store, {})
    return config, store, lifecycle, Execution(lifecycle, Context(root), enabled=True)


async def commands(store):
    async with store.database.connect() as connection:
        return list((await connection.execute(sa.select(runtime_commands))).mappings())


async def assert_empty(store):
    async with store.transaction() as tx:
        for collection in ("attempts", "attempt_bindings", "sessions", "operations"):
            assert await tx.all(collection) == []
        assert (await tx.get("workspaces", "ws_test"))["snapshot"]["active_attempt_ids"] == []
    assert await commands(store) == []


def test_duplicates_conflicts_restart_and_queries(tmp_path):
    async def scenario():
        config, store, lifecycle, service = await setup(tmp_path)
        original = request()
        replies = await asyncio.gather(*(service.execute(original) for _ in range(8)))
        assert replies == [replies[0]] * 8 and replies[0]["status"] == "accepted"
        reply = replies[0]
        wire = await commands(store)
        assert len(wire) == 1
        assert isinstance(wire[0]["wire"], bytes)
        message = json.loads(wire[0]["wire"])
        assert message["target"]["attempt_id"] == reply["resource_id"]
        async with store.transaction() as tx:
            assert len(await tx.all("attempts")) == len(await tx.all("sessions")) == 1
            assert await tx.pending_allocations(10) == []
        await lifecycle.reconcile()
        page = await service.list(query())
        session = page["items"][0]
        assert session["attempts"][0]["state"] == "starting"
        envelopes.check(f"{SCHEMAS}/session.schema.json#/$defs/session", session)
        changed = deepcopy(original)
        changed.body["payload"]["task"]["instruction"] = "Conflict"
        with pytest.raises(ServiceError) as caught:
            await service.execute(changed)
        assert caught.value.code == "COMMAND_ID_CONFLICT"
        await store.close()
        store = DurableStore(PERSISTENCE.database(config))
        service = Execution(Lifecycle(store, {}), enabled=False)
        expired = deepcopy(original)
        expired.body["deadline_at"] = "2000-01-01T00:00:00Z"
        assert await service.execute(expired) == reply
        assert await commands(store) == wire
        assert await service.list(query()) == page
        get = query()
        get.parameters["session_id"] = session["session_id"]
        assert await service.get(get) == {"session": session}
        assert service.commands() == {}
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["disabled", "runtime", "ownership", "registration", "executor", "settings", "missing", "traversal", "absolute", "symlink", "reference", "generation", "capacity", "expired"])
def test_rejections_leave_no_partial_acceptance(tmp_path, invalid):
    async def scenario():
        _, store, _, service = await setup(tmp_path)
        req = request()
        if invalid == "disabled":
            service.enabled = False
        elif invalid in ("runtime", "ownership"):
            async with store.transaction() as tx:
                if invalid == "ownership":
                    await tx.delete("workspace_runtimes", "ws_test")
                else:
                    record = await tx.get("workspaces", "ws_test")
                    record["snapshot"]["runtime"]["state"] = "stopped"
                    await tx.put("workspaces", "ws_test", record)
        elif invalid in ("registration", "generation", "capacity"):
            async with store.transaction() as tx:
                status = await tx.get("runtime_status", "rt_test")
                status.update({"registration": {"ready": False}, "generation": {"generation": 2}, "capacity": {"max_concurrent_attempts": 0}}[invalid])
                await tx.put("runtime_status", "rt_test", status)
        elif invalid == "executor":
            req.body["payload"]["agent"]["runner"] = "unknown"
        elif invalid == "settings":
            req.body["payload"]["agent"]["settings"] = {"bad": True}
        elif invalid == "expired":
            req.body["deadline_at"] = "2000-01-01T00:00:00Z"
        else:
            if invalid == "symlink":
                (service.context.root / "escape").symlink_to(tmp_path)
            if invalid == "reference":
                service.context.value["references"] = [{"uri": "file:///secret"}]
            else:
                service.context.value["working_directory"] = {"missing": "missing", "traversal": "..", "absolute": str(tmp_path), "symlink": "escape"}[invalid]
        reply = await service.execute(req)
        assert reply["status"] == "rejected"
        await assert_empty(store)
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["outbox", "receipt"])
def test_storage_failure_rolls_back_acceptance(tmp_path, monkeypatch, failure):
    async def scenario():
        _, store, _, service = await setup(tmp_path)
        method = "append_runtime_command" if failure == "outbox" else "record_command"
        original = getattr(Transaction, method)
        async def fail(self, *args):
            await original(self, *args)
            raise RuntimeError()
        monkeypatch.setattr(Transaction, method, fail)
        with pytest.raises(RuntimeError):
            await service.execute(request())
        await assert_empty(store)
        monkeypatch.setattr(Transaction, method, original)
        assert (await service.execute(request()))["status"] == "accepted"
        await store.close()
    asyncio.run(scenario())


def test_sessions_parallel_continuation_pagination_and_transitions(tmp_path):
    async def scenario():
        _, store, lifecycle, service = await setup(tmp_path)
        replies = await asyncio.gather(*(service.execute(request(f"cmd_{n}")) for n in range(3)))
        page = await service.list(query(limit=1))
        assert len(page["items"]) == 1 and page["next_cursor"]
        second = await service.list(query(limit=2, cursor=page["next_cursor"]))
        assert len(second["items"]) == 2
        assert second["next_cursor"] is None
        session = page["items"][0]
        busy = request("cmd_busy")
        busy.body["payload"]["session"] = {"mode": "continue", "session_id": session["session_id"]}
        assert (await service.execute(busy))["error"]["code"] == "INVALID_STATE"
        message = json.loads((await commands(store))[0]["wire"])
        ack = {"command_id": message["command_id"], "target": message["target"], "status": "accepted"}
        assert validate_message("command_ack", ack, (await commands(store))[0]["subject"]) == []
        with pytest.raises(InvalidTransition):
            async with store.transaction() as tx:
                await service.transition(tx, message["target"]["attempt_id"], ack["status"])
        for reply in replies:
            assert (await store.read("operations", reply["operation_id"]))["snapshot"]["status"] == "pending"
        # A validated runtime receipt is only delivery evidence. There is no
        # acknowledgement handler that could mark these operations successful.
        attempt_id = session["attempts"][0]["attempt_id"]
        attempt_record = await store.read("attempts", attempt_id)
        async with store.transaction() as tx:
            await service.transition(tx, attempt_id, "running")
        operation = (await store.read("operations", attempt_record["operation_id"]))["snapshot"]
        assert operation["status"] == "succeeded"
        envelopes.check(f"{SCHEMAS}/operations.schema.json#/$defs/operation", operation)
        failure = {"code": "PROVIDER_ERROR", "message": "fixture failure", "retryable": False}
        async with store.transaction() as tx:
            await service.transition(tx, attempt_id, "failed", error=failure)
        assert (await store.read("operations", attempt_record["operation_id"]))["snapshot"] == operation
        with pytest.raises(InvalidTransition):
            async with store.transaction() as tx:
                await service.transition(tx, attempt_id, "running")
        continued = request("cmd_continue", payload=busy.body["payload"])
        continuations = await asyncio.gather(*(service.execute(request(f"cmd_continue_{n}", payload=continued.body["payload"])) for n in range(4)))
        assert [r["status"] for r in continuations].count("accepted") == 1
        # Failure before start fails execute; it still releases only its attempt.
        pending = replies[0]
        if pending["resource_id"] == attempt_id:
            pending = replies[1]
        async with store.transaction() as tx:
            await service.transition(tx, pending["resource_id"], "failed", error=failure)
        assert (await store.read("operations", pending["operation_id"]))["snapshot"]["status"] == "failed"
        workspace = (await store.read("workspaces", "ws_test"))["snapshot"]
        assert workspace["state"] == "ready" and len(workspace["active_attempt_ids"]) == 2
        events = await store.pending_events(10)
        assert [e.envelope["event_type"] for e in events] == ["task.started", "task.failed", "task.failed"]
        await lifecycle.reconcile()
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["execute", "destroy", "concurrent"])
def test_destroy_and_execute_share_atomic_admission(tmp_path, first):
    async def scenario():
        _, store, lifecycle, service = await setup(tmp_path)
        destroy = request("cmd_destroy", {"retention": {"artifacts": "delete", "checkpoints": "delete"}}, "destroy")
        if first == "execute":
            execute_reply = await service.execute(request())
            destroy_reply = await lifecycle.destroy(destroy)
        elif first == "destroy":
            destroy_reply = await lifecycle.destroy(destroy)
            execute_reply = await service.execute(request())
        else:
            execute_reply, destroy_reply = await asyncio.gather(service.execute(request()), lifecycle.destroy(destroy))
        assert sorted([execute_reply["status"], destroy_reply["status"]]) == ["accepted", "rejected"]
        if first == "execute":
            assert execute_reply["status"] == "accepted"
        elif first == "destroy":
            assert destroy_reply["status"] == "accepted"
        workspace = await store.read("workspaces", "ws_test")
        if execute_reply["status"] == "accepted":
            assert destroy_reply["error"]["code"] == "INVALID_STATE"
            assert workspace["snapshot"]["state"] == "ready"
            assert workspace["snapshot"]["active_attempt_ids"] == [execute_reply["resource_id"]]
            assert not workspace.get("active_operation_id")
        else:
            assert execute_reply["error"]["code"] == "UNAVAILABLE"
            assert workspace["snapshot"]["state"] == "destroying"
            assert workspace["snapshot"]["active_attempt_ids"] == []
            assert workspace["active_operation_id"] == destroy_reply["operation_id"]
            assert await commands(store) == []
        await store.close()
    asyncio.run(scenario())


def test_public_routes_validate_acceptance_and_session_snapshots(tmp_path):
    from openengine.messaging import Server

    async def scenario():
        _, store, lifecycle, service = await setup(tmp_path)
        server = Server(None, "vcp", "nats://127.0.0.1:4222", commands={**lifecycle.commands(), **service.commands()},
                        queries={**lifecycle.queries(), **service.queries()})
        req = request()
        response = json.loads(await server.handle(req.subject, envelopes.encode(req.body)))
        assert response["status"] == "accepted"
        page = json.loads(await server.handle("oe.v1.vcp.query.workspaces.ws_test.sessions.list", b'{"params":{}}'))
        assert page["status"] == "ok"
        session = page["result"]["items"][0]
        result = json.loads(await server.handle(f"oe.v1.vcp.query.sessions.{session['session_id']}.get", b'{"params":{}}'))
        assert result == {"status": "ok", "result": {"session": session}}
        disabled = Server(None, "vcp", "nats://127.0.0.1:4222", commands={**lifecycle.commands(), **Execution(lifecycle).commands()})
        assert json.loads(await disabled.handle(req.subject, envelopes.encode(request("cmd_disabled").body)))["status"] == "rejected"
        async with store.transaction() as tx:
            assert len(await tx.all("attempts")) == 1
        await store.close()
    asyncio.run(scenario())


def test_accepted_binding_and_command_cannot_be_overwritten(tmp_path):
    from sqlalchemy.exc import IntegrityError

    async def scenario():
        _, store, _, service = await setup(tmp_path)
        reply = await service.execute(request())
        wire = (await commands(store))[0]
        with pytest.raises(IntegrityError):
            async with store.transaction() as tx:
                await tx.append_runtime_command(wire["command_id"], reply["resource_id"], wire["subject"], b'{}')
        with pytest.raises(IntegrityError):
            async with store.transaction() as tx:
                await tx.put("attempt_bindings", reply["resource_id"], {})
        with pytest.raises(KeyError):
            async with store.transaction() as tx:
                await tx.delete("attempt_bindings", reply["resource_id"])
        assert (await commands(store))[0] == wire
        await store.close()
    asyncio.run(scenario())


def test_completion_requires_result_before_mutation(tmp_path):
    async def scenario():
        _, store, _, service = await setup(tmp_path)
        reply = await service.execute(request())
        attempt_id = reply["resource_id"]
        async with store.transaction() as tx:
            await service.transition(tx, attempt_id, "running")
        before = await store.read("attempts", attempt_id)
        async with store.transaction() as tx:
            with pytest.raises(InvalidTransition):
                await service.transition(tx, attempt_id, "completed")
            assert await tx.get("attempts", attempt_id) == before
        async with store.transaction() as tx:
            await service.transition(tx, attempt_id, "completed", result={})
        assert (await store.read("attempts", attempt_id))["snapshot"]["state"] == "completed"
        assert (await store.read("operations", reply["operation_id"]))["snapshot"]["status"] == "succeeded"
        assert [event.envelope["event_type"] for event in await store.pending_events(10)] == ["task.started", "task.completed"]
        await store.close()

    asyncio.run(scenario())


def test_session_pages_use_indexes_without_writer_reservation(tmp_path):
    async def scenario():
        _, store, _, service = await setup(tmp_path)
        async with store.transaction() as tx:
            for i in range(300):
                await tx.put("sessions", f"session_{i:04}", {"snapshot": {
                    "session_id": f"session_{i:04}", "workspace_id": "ws_test" if i < 299 else "ws_other",
                    "created_at": "2026-01-01T00:00:00Z", "state": "active" if i % 2 else "idle"}})
            for state, index in [("", "ix_sessions_workspace_page"),
                                 (" AND json_extract(body, '$.snapshot.state') = 'active'", "ix_sessions_workspace_state_page")]:
                plan = await tx._connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN SELECT body FROM sessions "
                    "WHERE json_extract(body, '$.snapshot.workspace_id') = 'ws_test'" + state +
                    " AND (json_extract(body, '$.snapshot.created_at'), key) < ('2026-01-01T00:00:00Z', 'session_0299')"
                    " ORDER BY json_extract(body, '$.snapshot.created_at') DESC, key DESC LIMIT 2")
                assert index in str(plan.all())
        # A separate writer reservation must not block either query read.
        async with store.transaction():
            page = await asyncio.wait_for(service.list(query(limit=2)), 1)
        assert [s["session_id"] for s in page["items"]] == ["session_0298", "session_0297"]
        page = await service.list(query(limit=2, cursor=page["next_cursor"]))
        assert [s["session_id"] for s in page["items"]] == ["session_0296", "session_0295"]
        page = await service.list(query(limit=2, state=["active"]))
        assert [s["session_id"] for s in page["items"]] == ["session_0297", "session_0295"]
        page = await service.list(query(limit=2, state=["active", "idle"]))
        assert [s["session_id"] for s in page["items"]] == ["session_0298", "session_0297"]
        await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["destroy", "runtime", "ownership", "readiness", "capacity", "session", "unrelated"])
def test_preparation_releases_writer_and_revalidates(tmp_path, change):
    async def scenario():
        _, store, lifecycle, service = await setup(tmp_path)
        req = request()
        if change == "session":
            reply = await service.execute(req)
            async with store.transaction() as tx:
                await service.transition(tx, reply["resource_id"], "running")
                await service.transition(tx, reply["resource_id"], "completed", result={})
            session = (await service.list(query()))["items"][0]
            req = request("cmd_continue", payload={**req.body["payload"],
                          "session": {"mode": "continue", "session_id": session["session_id"]}})
        entered, release = asyncio.Event(), asyncio.Event()
        prepare = service.context.prepare
        async def slow(*args):
            entered.set()
            await release.wait()
            return await prepare(*args)
        service.context.prepare = slow
        task = asyncio.create_task(service.execute(req))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            if change in ("destroy", "unrelated"):
                destroy = request("cmd_destroy", {"retention": {"artifacts": "delete", "checkpoints": "delete"}}, "destroy")
                if change == "unrelated":
                    async with store.transaction() as tx:
                        other = deepcopy(await tx.get("workspaces", "ws_test"))
                        other["snapshot"]["workspace_id"] = "ws_other"
                        await tx.put("workspaces", "ws_other", other)
                    destroy.parameters["workspace_id"] = "ws_other"
                    destroy = replace(destroy, subject=destroy.subject.replace("ws_test", "ws_other"))
                assert (await asyncio.wait_for(lifecycle.destroy(destroy), 2))["status"] == "accepted"
            else:
                async with store.transaction() as tx:
                    collection, key = {
                        "runtime": ("workspace_runtimes", "ws_test"),
                        "ownership": ("workspace_runtimes", "ws_test"),
                        "readiness": ("runtime_status", "rt_test"),
                        "capacity": ("runtime_status", "rt_test"),
                        "session": ("sessions", req.body["payload"]["session"].get("session_id", "")),
                    }[change]
                    value = await tx.get(collection, key)
                    if change == "ownership":
                        value["runtime_id"] = "rt_replaced"
                    elif change == "runtime":
                        value["locator"] = {"path": "changed"}
                    elif change == "readiness":
                        value["ready"] = False
                    elif change == "capacity":
                        value["max_concurrent_attempts"] = 0
                    else:
                        value["snapshot"]["state"] = "active"
                    await tx.put(collection, key, value)
        finally:
            release.set()
        reply = await task
        assert reply["status"] == ("accepted" if change == "unrelated" else "rejected")
        if change != "unrelated":
            assert reply["error"]["code"] == ("INVALID_STATE" if change in ("session", "ownership") else "UNAVAILABLE")
        await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("context", [{}, None, {"references": []},
    {"references": [], "working_directory": 7, "instruction_files": []},
    {"references": [], "working_directory": ".", "instruction_files": None}])
def test_malformed_context_records_rejection(tmp_path, context):
    async def scenario():
        _, store, _, service = await setup(tmp_path)
        service.context.value = context
        req = request()
        reply = await service.execute(req)
        assert reply["status"] == "rejected"
        assert reply["error"]["code"] == "INVALID_ARGUMENT"
        await assert_empty(store)
        service.context.value = {"working_directory": ".", "instruction_files": ["AGENTS.md"], "references": []}
        assert await service.execute(req) == reply
        await store.close()
    asyncio.run(scenario())
