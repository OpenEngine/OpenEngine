"""Fake-only durable steering, races and history; no authenticated model execution."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from openengine.messaging import Client, Request, Server, ServiceError, envelopes
from openengine.messaging.tenancy import Principal
from openengine.persistence import DatabaseConfig

from virtual_control_plane.application.execution import Execution
from virtual_control_plane.application.lifecycle import Lifecycle
from virtual_control_plane.application.runtime_delivery import RuntimeCommands, RuntimeUpdates
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore, Transaction
from virtual_control_plane.tests.test_cancellation import CANCEL, Flow, Local, Vcp, deployment, destroy
from virtual_control_plane.tests.test_runtime_delivery import (
    EXECUTE, RUNTIME, WORKSPACE, HostContext, binary, commands, daemon, drain, events, payload, ready, settled, until,
)
from workspace_daemon.executor import ExecutionWorker, FakeExecutor
from workspace_daemon.journal import encode
from workspace_daemon.outbox import UpdateOutbox

__all__ = ["binary"]

STEER = f"oe.v1.vcp.control.workspaces.{WORKSPACE}.steer"


class Controlled(FakeExecutor):
    """The test decides exactly when the fake's completion races the writer."""
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def wait(self, *args):
        await self.release.wait()
        return "completed", {"result": {"summary": "Done"}}, "exited"


class SteeringLocal(Local):
    async def send(self, subject, wire):
        if subject.endswith(".steer") and "steer" not in self.drop:
            self.sent.append((subject, wire))
            reply = encode(await self.journal.steer(subject, json.loads(wire)))
            if "steer" in self.lose:
                from openengine.messaging import Unavailable
                self.lose.remove("steer")
                raise Unavailable(subject, "reply lost")
            return reply
        return await super().send(subject, wire)


class SteeringFlow(Flow):
    async def start(self, tmp_path, executor=None):
        await super().start(tmp_path, executor or Controlled())
        self.server = Server(None, "vcp", "nats://127.0.0.1:4222",
                             commands={**self.execution.lifecycle.commands(), **self.execution.commands()},
                             queries=self.execution.queries())
        self.local = SteeringLocal(self.journal, RuntimeUpdates(self.store, self.execution))
        self.dispatcher = RuntimeCommands(self.store, self.local, self.execution)
        self.outbox = UpdateOutbox(self.journal, self.local)
        return self

    async def running(self):
        execute = await self.execute(delay_seconds=30)
        await drain(self.dispatcher, self.worker)
        async def started():
            async with self.journal.store.read() as tx:
                return (await tx.attempt(execute["resource_id"]))["state"] == "running"
        await until(started)
        await drain(self.outbox)
        self.attempt_id = execute["resource_id"]
        self.session_id = (await self.attempt(self.attempt_id))["session_id"]
        return execute

    async def steer(self, message="First", command_id="cmd_steer"):
        return await self.command(STEER, envelopes.command({"session_id": self.session_id, "message": message},
                                                          command_id=command_id))

    async def history(self, **params):
        subject = f"oe.v1.vcp.query.sessions.{self.session_id}.events.list"
        return json.loads(await self.server.handle(subject, envelopes.encode(envelopes.query(params))))

    async def complete(self):
        self.journal.executors["fake"].release.set()
        await asyncio.gather(*self.worker.tasks.values())
        await drain(self.outbox)

    async def reopen_vcp(self, tmp_path):
        await self.store.close()
        self.store = DurableStore(PERSISTENCE.database(DatabaseConfig(sqlite_directory=tmp_path / "vcp")))
        self.execution = Execution(Lifecycle(self.store, {}), HostContext(), enabled=True)
        self.server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=self.execution.commands(),
                             queries=self.execution.queries())
        self.local.updates = RuntimeUpdates(self.store, self.execution)
        self.dispatcher = RuntimeCommands(self.store, self.local, self.execution)


async def assert_outcome(flow, reply, outcome):
    operation = await flow.operation(reply["operation_id"])
    assert operation["status"] == ("succeeded" if outcome == "applied" else "failed")
    public = [e for e in await events(flow.store) if e["event_type"].startswith("steering.")
              and e["payload"]["operation"]["operation_id"] == reply["operation_id"]]
    assert len(public) == 1 and public[0]["event_type"] == "steering." + outcome
    history = (await flow.history())["result"]["items"]
    entries = [e for e in history if e.get("operation_id") == reply["operation_id"]]
    assert len(entries) == 1 and entries[0]["status"] == outcome
    async with flow.store.transaction() as tx:
        assert await tx.pending_steering(flow.attempt_id) == []


def test_retry_lost_ack_ordered_effects_and_final_result(tmp_path):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        first = await flow.steer()
        assert first["status"] == "accepted"
        assert await flow.steer() == first
        assert (await flow.steer("Changed"))["error"]["code"] == "COMMAND_ID_CONFLICT"
        second = await flow.steer("Second", "cmd_second")
        flow.local.lose.add("steer")
        await drain(flow.dispatcher, flow.outbox)
        await assert_outcome(flow, first, "applied")
        await assert_outcome(flow, second, "applied")
        rows = [json.loads(row["wire"]) for row in await commands(flow.store) if json.loads(row["wire"])["kind"] == "steer"]
        for command in rows:
            subject = f"oe.internal.v1.runtime.runtimes.{RUNTIME}.steer"
            assert (await flow.journal.steer(subject, command))["status"] == "accepted"
        await drain(flow.outbox)
        # Exact duplicate updates commit without another operation/event/history effect.
        import sqlalchemy as sa
        from workspace_daemon import store as journal_store
        async with flow.journal.store.read() as tx:
            wires = (await tx._connection.execute(sa.select(journal_store.updates.c.wire))).scalars().all()
        for wire in wires:
            if json.loads(wire)["kind"] == "steering.applied":
                assert json.loads(await flow.local.updates.receive(flow.journal.updates, wire))["status"] == "committed"
        await flow.complete()
        assert (await flow.attempt(flow.attempt_id))["result"] == {"summary": "Done\nFirst\nSecond"}
        await assert_outcome(flow, first, "applied")
        await flow.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("race", ["complete_undelivered", "cancel_undelivered", "cancel_before_application", "applied_before_cancel"])
def test_terminal_races_settle_without_changing_task_outcome(tmp_path, race):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        reply = await flow.steer()
        if race == "applied_before_cancel":
            await drain(flow.dispatcher, flow.outbox)
        if race.startswith("cancel") or race == "applied_before_cancel":
            await flow.cancel(flow.attempt_id)
            flow.local.drop.add("steer")
            await flow.dispatcher.run_once()  # cancel wins the daemon writer
            flow.local.drop.clear()
            if race == "cancel_before_application":
                await drain(flow.dispatcher, flow.outbox)
        await flow.complete()
        outcome = "applied" if race == "applied_before_cancel" else "failed"
        await assert_outcome(flow, reply, outcome)
        attempt = await flow.attempt(flow.attempt_id)
        assert attempt["state"] == ("completed" if race == "complete_undelivered" else "cancelled")
        if attempt["state"] == "completed":
            assert attempt["result"] == {"summary": "Done"}
        assert not [r for r in await commands(flow.store) if r["status"] == "pending"]
        assert (await flow.destroy("cmd_destroy"))["status"] == "accepted"
        # A delayed command arriving after terminal projection cannot reverse failure.
        command = next(json.loads(row["wire"]) for row in await commands(flow.store)
                       if json.loads(row["wire"])["kind"] == "steer")
        await flow.journal.steer(f"oe.internal.v1.runtime.runtimes.{RUNTIME}.steer", command)
        await drain(flow.outbox)
        await assert_outcome(flow, reply, outcome)
        await flow.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["accepted", "applied", "projected"])
def test_restart_never_reapplies_or_retargets(tmp_path, when):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        reply = await flow.steer()
        command = next(json.loads(row["wire"]) for row in await commands(flow.store)
                       if json.loads(row["wire"])["kind"] == "steer")
        if when != "accepted":
            if when == "applied":
                flow.local.lose.add("steer")  # application committed, acknowledgement never reached VCP
            await flow.dispatcher.run_once()
        if when == "projected":
            await drain(flow.outbox)
        await flow.worker.close()
        await flow.journal.store.close()
        await flow.reopen_vcp(tmp_path)
        flow.journal = await daemon(tmp_path, tmp_path / "root")
        flow.local.journal = flow.journal
        flow.outbox = UpdateOutbox(flow.journal, flow.local)
        flow.worker = ExecutionWorker(flow.journal)
        await flow.journal.recover()  # fences the interrupted attempt, never relaunches
        await drain(flow.outbox, flow.dispatcher)
        await assert_outcome(flow, reply, "failed" if when == "accepted" else "applied")
        assert (await flow.attempt(flow.attempt_id))["state"] == "failed"
        assert await flow.steer() == reply  # replay preserves original attempt even after fencing
        next_reply = await flow.command(EXECUTE, envelopes.command(payload("continue", flow.session_id, delay_seconds=30), command_id="cmd_next"))
        assert next_reply["status"] == "accepted", next_reply
        executor = Controlled()
        executor.release.set()
        flow.journal.executors["fake"] = executor
        await flow.settle()
        assert (await flow.attempt(next_reply["resource_id"]))["result"]["summary"] == "Done"
        await flow.journal.steer(f"oe.internal.v1.runtime.runtimes.{RUNTIME}.steer", command)
        await drain(flow.outbox)
        await assert_outcome(flow, reply, "failed" if when == "accepted" else "applied")
        await flow.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change,code", [
    ("codex", "UNSUPPORTED_CAPABILITY"), ("capability", "UNSUPPORTED_CAPABILITY"),
    ("idle", "INVALID_STATE"), ("starting", "INVALID_STATE"), ("cancelling", "INVALID_STATE"),
    ("closed", "INVALID_STATE"), ("stale", "INVALID_STATE"), ("owner", "INVALID_STATE"),
    ("restore", "UNSUPPORTED_CAPABILITY"),
])
def test_rejects_before_acceptance_or_executor_effects(tmp_path, change, code):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        async with flow.store.transaction() as tx:
            record = await tx.get("sessions", flow.session_id)
            if change == "codex":
                record["snapshot"]["agent"]["runner"] = "codex"
            elif change in ("idle", "closed"):
                record["snapshot"]["state"] = change
            elif change in ("starting", "cancelling"):
                record["snapshot"]["attempts"][0]["state"] = change
            elif change == "stale":
                record["snapshot"]["runtime_generation"] += 1
            elif change in ("owner", "restore"):
                workspace = await tx.get("workspaces", WORKSPACE)
                if change == "owner":
                    workspace["snapshot"]["runtime"]["runtime_id"] = "rt_other"
                else:
                    workspace["snapshot"]["state"] = "suspended"
                await tx.put("workspaces", WORKSPACE, workspace)
            elif change == "capability":
                status = await tx.get("runtime_status", RUNTIME)
                status["steering_executors"] = []
                await tx.put("runtime_status", RUNTIME, status)
            await tx.put("sessions", flow.session_id, record)
        before = await commands(flow.store)
        assert (await flow.steer())["error"]["code"] == code
        assert await commands(flow.store) == before
        async with flow.store.transaction() as tx:
            assert await tx.all("steering_commands") == []
        await flow.close()
    asyncio.run(scenario())


def test_acceptance_and_projection_roll_back_as_units(tmp_path, monkeypatch):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        append = Transaction.append_runtime_command
        async def fail_append(*args):
            raise RuntimeError("storage failed")  # noqa: TRY003 -- injected storage fault
        monkeypatch.setattr(Transaction, "append_runtime_command", fail_append)
        assert (await flow.steer())["error"]["code"] == "INTERNAL"
        async with flow.store.transaction() as tx:
            assert await tx.all("steering_commands") == []
        monkeypatch.setattr(Transaction, "append_runtime_command", append)
        reply = await flow.steer()
        assert reply["status"] == "accepted"
        await flow.dispatcher.run_once()
        append_history = Transaction.append_history
        monkeypatch.setattr(Transaction, "append_history", fail_append)
        with pytest.raises(RuntimeError, match="storage failed"):
            await flow.outbox.run_once()
        assert (await flow.operation(reply["operation_id"]))["status"] == "pending"
        assert [e["event_type"] for e in await events(flow.store)] == ["task.started"]
        monkeypatch.setattr(Transaction, "append_history", append_history)
        await drain(flow.outbox)
        await assert_outcome(flow, reply, "applied")
        await flow.close()
    asyncio.run(scenario())


async def append_message(flow, message_id):
    async with flow.store.transaction() as tx:
        await flow.execution.progress(tx, flow.attempt_id, {
            "kind": "conversation", "message_id": message_id, "role": "agent", "text": message_id})


def test_history_mixed_attempts_filters_pages_concurrent_append_and_restart(tmp_path):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        first_attempt = flow.attempt_id
        await append_message(flow, "msg_one")
        await flow.steer()
        await drain(flow.dispatcher, flow.outbox)
        await append_message(flow, "msg_two")
        first = (await flow.history(limit=1))["result"]
        cursor = first["next_cursor"]
        assert cursor and first["items"][0]["sequence"] == 1
        await flow.reopen_vcp(tmp_path)
        second = (await flow.history(limit=1, cursor=cursor))["result"]
        assert second["items"][0]["kind"] == "steering"
        # Concurrent append after page one stays reachable using the original cursor.
        await append_message(flow, "msg_three")
        await flow.complete()
        flow.journal.executors["fake"].release.clear()
        reply = await flow.command(EXECUTE, envelopes.command(payload("continue", flow.session_id, delay_seconds=30),
                                                              command_id="cmd_continue"))
        await drain(flow.dispatcher, flow.worker)
        flow.attempt_id = reply["resource_id"]
        async def started():
            async with flow.journal.store.read() as tx:
                return (await tx.attempt(flow.attempt_id))["state"] == "running"
        await until(started)
        await drain(flow.outbox)
        await append_message(flow, "msg_four")
        pages = first["items"][:]
        while cursor:
            page = (await flow.history(limit=1, cursor=cursor))["result"]
            pages += page["items"]
            cursor = page["next_cursor"]
        assert [item["sequence"] for item in pages] == [1, 2, 3, 4, 5]
        for kinds in ([], ["message"], ["steering"], ["steering", "message"], ["usage"]):
            for attempt in (None, first_attempt, flow.attempt_id):
                params = {**({"kind": kinds} if kinds else {}), **({"attempt_id": attempt} if attempt else {})}
                actual, cursor = [], None
                while True:
                    page = (await flow.history(**params, limit=1, **({"cursor": cursor} if cursor else {})))["result"]
                    actual.extend(page["items"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                assert actual == [item for item in pages if (not kinds or item["kind"] in kinds)
                                  and (not attempt or item["attempt_id"] == attempt)]
        # Normalization makes filter order irrelevant.
        page = (await flow.history(kind=["message", "steering"], limit=1))["result"]
        assert (await flow.history(kind=["steering", "message"], cursor=page["next_cursor"]))["result"]["items"] == pages[1:]
        await flow.close()
    asyncio.run(scenario())


def test_history_cursors_reject_malformed_mismatched_and_foreign_scope(tmp_path):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        await append_message(flow, "msg_one")
        await append_message(flow, "msg_two")
        cursor = (await flow.history(limit=1))["result"]["next_cursor"]
        for value in ("garbage", "v2_" + cursor[3:], "v1_" + "0" * 32, cursor + "x"):
            assert (await flow.history(cursor=value))["error"]["code"] == "INVALID_CURSOR"
        for params in ({"kind": ["steering"]}, {"attempt_id": "attempt_other"}):
            assert (await flow.history(cursor=cursor, **params))["error"]["code"] == "INVALID_CURSOR"
        request = Request("query", "query", "", envelopes.query({"cursor": cursor}), "", {"session_id": flow.session_id},
                          Principal("00000000-0000-7000-8000-000000000001"))
        for changed in (replace(request, parameters={"session_id": "session_other"}),
                        replace(request, principal=Principal("00000000-0000-7000-8000-000000000002"))):
            with pytest.raises(ServiceError) as caught:
                await flow.execution.steering.history(changed)
            assert caught.value.code == "INVALID_CURSOR"
        await flow.close()
    asyncio.run(scenario())


def test_steering_and_history_cursor_survive_vcp_restart_over_real_nats(binary, tmp_path):
    async def scenario():
        async with deployment(binary, tmp_path) as (connection, config, process):
            vcp = await Vcp(connection, config).start()
            await process.start(initialize=True)
            await until(lambda: ready(vcp))
            client = Client(connection)
            execute = await client.command(EXECUTE, payload(delay_seconds=30), command_id="cmd_execute")
            await until(lambda: settled(vcp, execute["resource_id"], "running"))
            session_id = (await vcp.store.read("attempts", execute["resource_id"]))["snapshot"]["session_id"]
            body = {"session_id": session_id, "message": "First"}
            first = await client.command(STEER, body, command_id="cmd_first")
            assert await client.command(STEER, body, command_id="cmd_first") == first
            second = await client.command(STEER, {**body, "message": "Second"}, command_id="cmd_second")
            async def applied():
                return (await vcp.store.read("operations", second["operation_id"]))["snapshot"]["status"] == "succeeded"
            await until(applied)
            subject = f"oe.v1.vcp.query.sessions.{session_id}.events.list"
            page = (await client.query(subject, {"kind": ["steering"], "limit": 1}))["result"]
            assert page["items"][0]["message"] == "First" and page["next_cursor"]
            await vcp.stop()
            vcp = await Vcp(connection, config).start()
            page = (await client.query(subject, {"kind": ["steering"], "cursor": page["next_cursor"], "limit": 1}))["result"]
            assert page["items"][0]["message"] == "Second" and page["next_cursor"] is None
            await client.command(CANCEL, {"attempt_id": execute["resource_id"]}, command_id="cmd_cancel")
            await until(lambda: settled(vcp, execute["resource_id"], "cancelled"))
            assert len([e for e in await events(vcp.store) if e["event_type"] == "steering.applied"]) == 2
            assert (await destroy(client, "cmd_destroy"))["status"] == "accepted"
            await vcp.stop()
    asyncio.run(scenario())


def test_history_cursor_retention_is_bounded_and_survives_restart(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from virtual_control_plane.persistence import durable

    timestamp = [1000000.0]
    monkeypatch.setattr(durable.time, "time", lambda: timestamp[0])

    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        await append_message(flow, "msg_one")
        await append_message(flow, "msg_two")
        cursors = [(await flow.history(limit=1))["result"]["next_cursor"] for _ in range(3)]
        async with flow.store.database.connect() as connection:
            assert (await connection.execute(sa.text("SELECT count(*) FROM history_cursors"))).scalar_one() == 3
        assert (await flow.history(cursor=cursors[0]))["result"]["items"]
        timestamp[0] += 24 * 60 * 60 + 1
        await flow.history(limit=1)
        assert (await flow.history(cursor=cursors[0]))["error"]["code"] == "INVALID_CURSOR"
        # A failed cursor insertion must not evict a usable cursor.
        with pytest.raises(RuntimeError):
            async with flow.store.transaction() as tx:
                await tx.save_history_cursor("v1_" + "0" * 32, {"scope": {}, "after": 1})
                raise RuntimeError("rollback")
        await flow.reopen_vcp(tmp_path)
        for cursor in [(await flow.history(limit=1))["result"]["next_cursor"]]:
            page = (await flow.history(cursor=cursor))["result"]
            assert [entry["sequence"] for entry in page["items"]] == [2]
        await flow.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("history_sequence", [None, 10])
def test_migration_preserves_preexisting_published_conversation_history(tmp_path, history_sequence):
    from openengine.persistence.migrations import downgrade

    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        await append_message(flow, "msg_old")
        await append_message(flow, "msg_old_second")
        expected = (await flow.history())["result"]["items"]
        # A downgrade alone retains the field absent in pre-upgrade documents.
        async with flow.store.transaction() as tx:
            stored = await tx.get("sessions", flow.session_id)
            if history_sequence is None:
                del stored["history_sequence"]
            else:
                stored["history_sequence"] = history_sequence
            await tx.put("sessions", flow.session_id, stored)
        for event in await flow.store.pending_events(100):
            await flow.store.mark_published(event.sequence)
        await flow.worker.close()
        await flow.store.close()
        config = DatabaseConfig(sqlite_directory=tmp_path / "vcp")
        downgrade(PERSISTENCE.migrations, PERSISTENCE.database(config), "cfed9cf520fd")
        PERSISTENCE.upgrade(config)
        await flow.reopen_vcp(tmp_path)
        assert (await flow.history())["result"]["items"] == expected
        await append_message(flow, "msg_new")
        next_sequence = max(history_sequence or 0, 2) + 1
        assert [entry["sequence"] for entry in (await flow.history())["result"]["items"]] == [1, 2, next_sequence]
        await flow.steer()
        await drain(flow.dispatcher, flow.outbox)
        sequences = [entry["sequence"] for entry in (await flow.history())["result"]["items"]]
        assert sequences == [1, 2, next_sequence, next_sequence + 1]
        await flow.close()
    asyncio.run(scenario())


def test_history_filter_combinations_use_ordered_index_seeks(tmp_path):
    import sqlalchemy as sa

    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        await append_message(flow, "msg_one")
        async with flow.store.database.connect() as connection:
            for where, index in [
                ("", "sqlite_autoindex_session_history"),
                (" AND kind = 'message'", "ix_session_history_kind"),
                (" AND attempt_id = :attempt", "ix_session_history_attempt"),
                (" AND attempt_id = :attempt AND kind = 'message'", "ix_session_history_attempt_kind"),
            ]:
                rows = await connection.execute(sa.text(
                    "EXPLAIN QUERY PLAN SELECT entry FROM session_history WHERE session_id = :session "
                    "AND sequence > 0" + where + " ORDER BY sequence LIMIT 2"),
                    {"session": flow.session_id, "attempt": flow.attempt_id})
                plan = " ".join(str(row) for row in rows)
                assert "SEARCH session_history USING INDEX " + index in plan
                assert "SCAN" not in plan and "TEMP B-TREE" not in plan
        await flow.close()
    asyncio.run(scenario())


def test_history_pages_bound_encoded_bytes(tmp_path):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        for i in range(17):
            async with flow.store.transaction() as tx:
                await flow.execution.progress(tx, flow.attempt_id, {
                    "kind": "conversation", "message_id": f"msg_{i}", "role": "agent", "text": "😀" * 65536})
        sequences, cursor = [], None
        while True:
            response = await flow.history(**({"cursor": cursor} if cursor else {}))
            assert len(envelopes.encode(response)) < 1024 * 1024
            page = response["result"]
            sequences.extend(item["sequence"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert sequences == list(range(1, 18))
        await flow.close()
    asyncio.run(scenario())


def test_downgrade_refuses_accepted_steering(tmp_path):
    from openengine.persistence.migrations import downgrade

    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        reply = await flow.steer()
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            downgrade(PERSISTENCE.migrations, PERSISTENCE.database(
                DatabaseConfig(sqlite_directory=tmp_path / "vcp")), "cfed9cf520fd")
        await drain(flow.dispatcher, flow.outbox)
        await assert_outcome(flow, reply, "applied")
        await flow.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [1, 50])
def test_history_single_entry_above_page_budget(tmp_path, limit):
    async def scenario():
        flow = await SteeringFlow().start(tmp_path)
        await flow.running()
        await append_message(flow, "msg_before")
        async with flow.store.transaction() as tx:
            await flow.execution.progress(tx, flow.attempt_id, {
                "kind": "conversation", "message_id": "msg_large", "role": "agent", "text": "😀" * 80000})
        await append_message(flow, "msg_after")
        sequences, cursor = [], None
        while True:
            response = await flow.history(limit=limit, **({"cursor": cursor} if cursor else {}))
            assert len(envelopes.encode(response)) < 1024 * 1024
            page = response["result"]
            assert page["items"]
            if any(item["message_id"] == "msg_large" for item in page["items"]):
                assert len(page["items"]) == 1
                assert len(envelopes.encode(page["items"][0])) > 900 * 1024
                assert page["items"][0]["text"] == "😀" * 80000
            sequences.extend(item["sequence"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert sequences == [1, 2, 3]
        await flow.close()
    asyncio.run(scenario())
