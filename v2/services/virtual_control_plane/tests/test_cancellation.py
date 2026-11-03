"""Public cancel through the workspace daemon: in-process races, then a daemon subprocess over real NATS.

The daemon runs only the in-process fake executor; no ACP subprocess, cgroup,
VM or model is involved here (see workspace_daemon/tests/test_cancellation.py
for the ACP fixture cases).
"""
from __future__ import annotations

import asyncio
import json
import signal
import subprocess
from contextlib import asynccontextmanager

from openengine.contracts.runtime import validate_wire_message
from openengine.messaging import Client, LocalOperator, Server, ServiceError, Unavailable, envelopes, listen_address

import pytest

from virtual_control_plane.application.execution import Execution, InvalidTransition
from virtual_control_plane.application.lifecycle import Lifecycle
from virtual_control_plane.application.runtime_delivery import RuntimeCommands, RuntimeUpdates
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.runtime_nats import NatsRuntimeTransport, RuntimeUpdateListener
from virtual_control_plane.tests.test_runtime_delivery import (
    EXECUTE, RUNTIME, WORKSPACE, Daemon, HostContext, binary, commands, daemon, drain, events, payload, ready,
    settled, until, vcp_store, workspace_root,
)
from virtual_control_plane.tests.test_lifecycle import broker
from workspace_daemon.executor import ExecutionWorker, FakeExecutor
from workspace_daemon.journal import encode
from workspace_daemon.outbox import UpdateOutbox

__all__ = ["binary"]  # the module-scoped NATS server fixture

CANCEL = f"oe.v1.vcp.control.workspaces.{WORKSPACE}.cancel"
DESTROY = f"oe.v1.vcp.control.workspaces.{WORKSPACE}.destroy"


class Local:
    """Every protocol direction in one process, through the same validated bytes as NATS; `drop` loses sends."""

    def __init__(self, journal, updates):
        self.journal, self.updates, self.sent, self.drop, self.lose = journal, updates, [], set(), set()

    async def send(self, subject, wire):
        action = subject.rsplit(".", 1)[-1]
        if action in self.drop:
            raise Unavailable(subject, "partitioned")
        self.sent.append((subject, wire))
        if action == "updates":
            if (reply := await self.updates.receive(subject, wire)) is None:
                raise Unavailable(subject, "discarded")
        else:
            assert validate_wire_message(action, wire, subject) == []
            handler = {"execute": self.journal.execute, "cancel": self.journal.cancel,
                       "inspect": self.journal.inspect}[action]
            reply = encode(await handler(subject, json.loads(wire)))
        if action in self.lose:
            self.lose.discard(action)
            raise Unavailable(subject, "reply lost")  # the effect committed; only the acknowledgement is lost
        return reply


class Flow:
    """VCP and a daemon journal wired in process, with explicit control over every worker."""

    async def start(self, tmp_path, executor=None):
        root = workspace_root(tmp_path)
        self.store, _ = await vcp_store(tmp_path, {"path": str(root)})
        lifecycle = Lifecycle(self.store, {})
        self.execution = Execution(lifecycle, HostContext(), enabled=True)
        self.server = Server(None, "vcp", "nats://127.0.0.1:4222",
                             commands={**lifecycle.commands(), **self.execution.commands()})
        self.journal = await daemon(tmp_path, root)
        if executor is not None:
            self.journal.executors = {"fake": executor}
        self.local = Local(self.journal, RuntimeUpdates(self.store, self.execution))
        self.dispatcher = RuntimeCommands(self.store, self.local, self.execution)
        self.outbox = UpdateOutbox(self.journal, self.local)
        self.worker = ExecutionWorker(self.journal, cancel_seconds=0.5)
        await self.journal.recover()
        await drain(self.outbox)
        return self

    async def command(self, subject, body):
        return json.loads(await self.server.handle(subject, envelopes.encode(body)))

    async def execute(self, command_id="cmd_exec", **settings):
        reply = await self.command(EXECUTE, envelopes.command(payload(**settings), command_id=command_id))
        assert reply["status"] == "accepted", reply
        return reply

    async def cancel(self, attempt_id, command_id="cmd_cancel", reason="Job cancelled"):
        return await self.command(CANCEL, envelopes.command({"attempt_id": attempt_id, "reason": reason},
                                                            command_id=command_id))

    async def destroy(self, command_id):
        return await self.command(DESTROY, envelopes.command({"retention": {"artifacts": "retain", "checkpoints": "delete"}},
                                                             command_id=command_id))

    async def settle(self):
        """Deliver, run and project until quiet; executor tasks are awaited between rounds."""
        for _ in range(3):
            await drain(self.dispatcher, self.worker, self.outbox)
            await asyncio.gather(*self.worker.tasks.values(), *self.worker.stopping.values(), return_exceptions=True)
        await drain(self.dispatcher, self.worker, self.outbox)

    async def attempt(self, attempt_id):
        return (await self.store.read("attempts", attempt_id))["snapshot"]

    async def operation(self, operation_id):
        return (await self.store.read("operations", operation_id))["snapshot"]

    async def workspace(self):
        return (await self.store.read("workspaces", WORKSPACE))["snapshot"]

    async def close(self):
        await self.worker.close()
        await self.store.close()
        await self.journal.store.close()


def kinds(values):
    return [event["event_type"] for event in values]


async def assert_cancelled(flow, execute, cancels, *, started):
    attempt_id = execute["resource_id"]
    assert (await flow.attempt(attempt_id))["state"] == "cancelled"
    assert (await flow.workspace())["active_attempt_ids"] == []
    published = await events(flow.store)
    assert kinds(published) == (["task.started"] if started else []) + ["task.cancelled"]
    cancelled = published[-1]
    winner = cancels[0]
    assert cancelled["payload"] == {"operation": {"operation_id": execute["operation_id"], "command_id": "cmd_exec"},
                                    "cancelled_by": {"operation_id": winner["operation_id"], "command_id": winner["command_id"]},
                                    "reason": "Job cancelled"}
    assert cancelled["causal_path"][:-1] == [winner["command_id"]] and cancelled["causal_path"][-1] == cancelled["event_id"]
    for reply in cancels:
        operation = await flow.operation(reply["operation_id"])
        assert (operation["status"], operation["result"]) == (
            "succeeded", {"attempt_id": attempt_id, "final_state": "cancelled"})
    operation = await flow.operation(execute["operation_id"])
    if started:
        assert operation["status"] == "succeeded"
    else:
        assert (operation["status"], operation["error"]["code"]) == ("failed", "INVALID_STATE")


def test_cancel_before_delivery_tombstones_and_late_execute_never_launches(tmp_path):
    async def scenario():
        flow = await Flow().start(tmp_path)
        execute = await flow.execute(delay_seconds=30)
        cancel = await flow.cancel(execute["resource_id"])
        assert cancel["status"] == "accepted" and (await flow.attempt(execute["resource_id"]))["state"] == "cancelling"
        assert (await flow.destroy("cmd_destroy_early"))["error"]["code"] == "INVALID_STATE"  # acceptance alone is not enough
        flow.local.drop.add("execute")  # the cancel overtakes the execute
        await drain(flow.dispatcher, flow.worker, flow.outbox)
        flow.local.drop.clear()
        await flow.settle()
        await assert_cancelled(flow, execute, [cancel], started=False)
        rows = {json.loads(row["wire"])["kind"]: row["status"] for row in await commands(flow.store)}
        assert rows == {"execute": "acknowledged", "cancel": "acknowledged"}
        assert not (tmp_path / "effects").exists()  # nothing was ever launched
        assert (await flow.destroy("cmd_destroy"))["status"] == "accepted"
        await flow.close()
    asyncio.run(scenario())


def test_cancel_of_a_recorded_attempt_never_launches(tmp_path):
    async def scenario():
        flow = await Flow().start(tmp_path)
        execute = await flow.execute(delay_seconds=30)
        await flow.dispatcher.run_once()  # the daemon records it; its worker has not run
        cancel = await flow.cancel(execute["resource_id"])
        await flow.settle()
        await assert_cancelled(flow, execute, [cancel], started=False)
        assert not (tmp_path / "effects").exists()
        await flow.close()
    asyncio.run(scenario())


def test_cancel_while_launching_reports_started_then_cancelled(tmp_path):
    class Slow(FakeExecutor):
        def __init__(self, effects):
            super().__init__(effects)
            self.release = asyncio.Event()

        async def launch(self, *args):
            await self.release.wait()
            return await super().launch(*args)

    async def scenario():
        executor = Slow(tmp_path / "effects")
        flow = await Flow().start(tmp_path, executor)
        execute = await flow.execute(delay_seconds=30)
        await flow.dispatcher.run_once()
        await flow.worker.run_once()
        await asyncio.sleep(0.05)
        async with flow.journal.store.read() as tx:
            assert (await tx.attempt(execute["resource_id"]))["state"] == "launching"
        cancel = await flow.cancel(execute["resource_id"])
        await flow.dispatcher.run_once()
        await flow.worker.run_once()
        executor.release.set()
        await flow.settle()
        await assert_cancelled(flow, execute, [cancel], started=True)
        assert (tmp_path / "effects").read_text().splitlines() == [execute["resource_id"]]
        await flow.close()
    asyncio.run(scenario())


def test_duplicate_cancels_and_lost_acknowledgements_settle_one_logical_outcome(tmp_path):
    async def scenario():
        flow = await Flow().start(tmp_path)
        execute = await flow.execute(delay_seconds=30)
        await flow.dispatcher.run_once()
        await flow.worker.run_once()
        await until(lambda: settled_daemon(flow, execute["resource_id"], "running"))
        await drain(flow.outbox)
        assert (await flow.attempt(execute["resource_id"]))["state"] == "running"
        first = await flow.cancel(execute["resource_id"])
        assert await flow.cancel(execute["resource_id"]) == first  # a public retry replays its receipt
        second = await flow.cancel(execute["resource_id"], "cmd_cancel_2")
        assert second["status"] == "accepted" and second["operation_id"] != first["operation_id"]
        cancels = [json.loads(row["wire"]) for row in await commands(flow.store) if b'"cancel"' in row["wire"]]
        assert len(cancels) == 1  # coalesced onto the winning cancel
        flow.local.lose = {"cancel", "updates"}
        await flow.settle()
        sent = [wire for subject, wire in flow.local.sent if subject.endswith(".cancel")]
        assert len(sent) == 2 and sent[0] == sent[1]  # the same bytes after a lost acknowledgement
        await assert_cancelled(flow, execute, [first, second], started=True)
        late = await flow.cancel(execute["resource_id"], "cmd_cancel_3")
        assert (await flow.operation(late["operation_id"]))["result"] == {
            "attempt_id": execute["resource_id"], "final_state": "cancelled"}
        assert kinds(await events(flow.store)).count("task.cancelled") == 1
        await flow.close()
    asyncio.run(scenario())


def test_cancelled_transition_without_the_winning_cancel_is_an_invalid_transition(tmp_path):
    async def scenario():
        flow = await Flow().start(tmp_path)
        execute = await flow.execute(delay_seconds=30)
        await flow.cancel(execute["resource_id"])
        for cancel in (None, {"command_id": "rtc_other", "cause": {}}):
            with pytest.raises(InvalidTransition):
                async with flow.store.transaction() as tx:
                    await flow.execution.transition(tx, execute["resource_id"], "cancelled", cancel=cancel)
        assert (await flow.attempt(execute["resource_id"]))["state"] == "cancelling"
        await flow.close()
    asyncio.run(scenario())


def test_completion_that_wins_the_race_keeps_its_outcome(tmp_path):
    async def scenario():
        flow = await Flow().start(tmp_path)
        execute = await flow.execute(delay_seconds=0)
        await flow.dispatcher.run_once()
        await flow.worker.run_once()
        await asyncio.gather(*flow.worker.tasks.values())  # completed in the daemon; VCP has not heard yet
        cancel = await flow.cancel(execute["resource_id"])
        assert (await flow.attempt(execute["resource_id"]))["state"] == "cancelling"
        await flow.settle()
        attempt = await flow.attempt(execute["resource_id"])
        assert attempt["state"] == "completed" and (await flow.workspace())["active_attempt_ids"] == []
        assert kinds(await events(flow.store)) == ["task.started", "task.completed"]
        assert (await flow.operation(cancel["operation_id"]))["result"] == {
            "attempt_id": execute["resource_id"], "final_state": "completed"}
        after = await flow.cancel(execute["resource_id"], "cmd_after")
        assert (await flow.operation(after["operation_id"]))["result"]["final_state"] == "completed"
        assert len(await commands(flow.store)) == 2  # nothing more to deliver for an ended attempt
        await flow.close()
    asyncio.run(scenario())


def test_unconfirmed_termination_keeps_destroy_refused_until_cancelled(tmp_path):
    class Stubborn(FakeExecutor):
        confirm = False

        async def fence(self, attempt):
            return self.confirm

    async def scenario():
        executor = Stubborn(tmp_path / "effects")
        flow = await Flow().start(tmp_path, executor)
        execute = await flow.execute(delay_seconds=30, ignore_cancel=True)
        await flow.dispatcher.run_once()
        await flow.worker.run_once()
        await until(lambda: settled_daemon(flow, execute["resource_id"], "running"))
        await flow.cancel(execute["resource_id"])
        await flow.dispatcher.run_once()
        await flow.worker.run_once()
        await asyncio.gather(*flow.worker.stopping.values(), return_exceptions=True)
        await flow.settle()  # the deadline passed and its task stopped, but fencing is not confirmed
        assert (await flow.attempt(execute["resource_id"]))["state"] == "cancelling"
        assert (await flow.workspace())["active_attempt_ids"] == [execute["resource_id"]]
        assert (await flow.destroy("cmd_destroy_early"))["error"]["code"] == "INVALID_STATE"
        executor.confirm = True
        await flow.settle()
        assert (await flow.attempt(execute["resource_id"]))["state"] == "cancelled"
        assert kinds(await events(flow.store)).count("task.cancelled") == 1
        assert (await flow.destroy("cmd_destroy"))["status"] == "accepted"
        await flow.close()
    asyncio.run(scenario())


def test_cancel_of_an_unknown_attempt_or_unowned_runtime_is_rejected(tmp_path):
    async def scenario():
        flow = await Flow().start(tmp_path)
        assert (await flow.cancel("attempt_missing"))["error"]["code"] == "NOT_FOUND"
        execute = await flow.execute(delay_seconds=30)
        async with flow.store.transaction() as tx:
            runtime = await tx.get("workspace_runtimes", WORKSPACE)
            await tx.put("workspace_runtimes", WORKSPACE, {**runtime, "generation": 2})
        reply = await flow.cancel(execute["resource_id"], "cmd_stale")
        assert reply["error"]["code"] == "INVALID_STATE"
        assert (await flow.attempt(execute["resource_id"]))["state"] == "starting"
        await flow.close()
    asyncio.run(scenario())


async def settled_daemon(flow, attempt_id, state):
    async with flow.journal.store.read() as tx:
        return (await tx.attempt(attempt_id))["state"] == state


# Real NATS, with the daemon as an ordinary host subprocess.


class Vcp:
    def __init__(self, connection, config):
        self.store = DurableStore(PERSISTENCE.database(config))
        lifecycle = Lifecycle(self.store, {})
        self.execution = Execution(lifecycle, HostContext(), enabled=True)
        self.server = Server(connection, "vcp", listen_address(connection),
                             commands={**lifecycle.commands(), **self.execution.commands()},
                             queries=self.execution.queries(), authorizer=LocalOperator())
        self.updates = RuntimeUpdates(self.store, self.execution)
        self.listener = RuntimeUpdateListener(connection, self.updates)
        self.dispatcher = RuntimeCommands(self.store, NatsRuntimeTransport(connection), self.execution, poll_seconds=0.05)

    async def start(self, deliver=True):
        await self.server.start()
        await self.listener.start()
        if deliver:
            self.dispatcher.start()
        return self

    async def stop(self):
        await self.dispatcher.close()
        await self.listener.close()
        await self.server.drain()
        await self.store.close()


@asynccontextmanager
async def deployment(binary, tmp_path):
    root = workspace_root(tmp_path)
    store, config = await vcp_store(tmp_path, {"path": str(root)})
    await store.close()
    async with broker(binary) as connection:
        process = Daemon(tmp_path, connection.connected_url.geturl(), root)
        try:
            yield connection, config, process
        finally:
            process.stop(signal.SIGKILL)


def children(pid):
    """Live processes whose parent is `pid` (the fake executor runs in process, so any would be a leak)."""
    result = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False)
    return result.stdout.split()


async def daemon_state(vcp, attempt_id):
    inspect = await vcp.dispatcher.inspect({"runtime_id": RUNTIME, "generation": 1}, WORKSPACE, [attempt_id])
    return inspect["result"]["attempts"][0].get("state")


async def destroy(client, command_id):
    try:
        return await client.command(DESTROY, {"retention": {"artifacts": "retain", "checkpoints": "delete"}},
                                    command_id=command_id)
    except ServiceError as error:
        return {"status": "rejected", "error": {"code": error.code}}


def test_daemon_killed_while_cancelling_recovers_the_intent_over_real_nats(binary, tmp_path):
    async def scenario():
        async with deployment(binary, tmp_path) as (connection, config, process):
            vcp = await Vcp(connection, config).start()
            await process.start(initialize=True)
            await until(lambda: ready(vcp))
            client = Client(connection)
            # Work that ignores the graceful request: the daemon holds it until its 5 s deadline.
            execute = await client.command(EXECUTE, payload(delay_seconds=30, ignore_cancel=True), command_id="cmd_exec")
            await until(lambda: settled(vcp, execute["resource_id"], "running"))
            cancel = await client.command(CANCEL, {"attempt_id": execute["resource_id"], "reason": "Job cancelled"},
                                          command_id="cmd_cancel")
            await until(lambda: daemon_is("cancelling", vcp, execute["resource_id"]))
            assert (await destroy(client, "cmd_destroy_early"))["error"]["code"] == "INVALID_STATE"
            # VCP and the daemon both stop before termination is confirmed.
            await vcp.stop()
            pid = process.process.pid
            process.stop(signal.SIGKILL)
            assert children(pid) == []
            await process.start()
            vcp = await Vcp(connection, config).start()
            await until(lambda: settled(vcp, execute["resource_id"], "cancelled"))
            published = await events(vcp.store)
            assert [e["event_type"] for e in published] == ["task.started", "task.cancelled"]
            assert published[-1]["payload"]["cancelled_by"] == {"operation_id": cancel["operation_id"], "command_id": "cmd_cancel"}
            assert (await vcp.store.read("operations", cancel["operation_id"]))["snapshot"]["result"]["final_state"] == "cancelled"
            assert launches(tmp_path) == [execute["resource_id"]]  # recovery fenced it; nothing was relaunched
            assert children(process.process.pid) == []
            assert (await destroy(client, "cmd_destroy"))["status"] == "accepted"
            await vcp.stop()
    asyncio.run(scenario())


def test_cancel_accepted_before_delivery_survives_vcp_and_daemon_restarts_over_real_nats(binary, tmp_path):
    async def scenario():
        async with deployment(binary, tmp_path) as (connection, config, process):
            vcp = await Vcp(connection, config).start(deliver=False)
            await process.start(initialize=True)
            await until(lambda: ready(vcp))
            client = Client(connection)
            execute = await client.command(EXECUTE, payload(delay_seconds=30), command_id="cmd_exec")
            cancel = await client.command(CANCEL, {"attempt_id": execute["resource_id"], "reason": "Job cancelled"},
                                          command_id="cmd_cancel")
            # Neither command was delivered; both processes restart.
            await vcp.stop()
            process.stop(signal.SIGKILL)
            await process.start()
            vcp = await Vcp(connection, config).start()
            await until(lambda: settled(vcp, execute["resource_id"], "cancelled"))
            await until(lambda: delivered(vcp))
            # A later VCP restart replays nothing new and duplicates nothing.
            await vcp.stop()
            vcp = await Vcp(connection, config).start()
            await asyncio.sleep(0.5)
            assert [e["event_type"] for e in await events(vcp.store)].count("task.cancelled") == 1
            assert (await vcp.store.read("operations", cancel["operation_id"]))["snapshot"]["status"] == "succeeded"
            assert launches(tmp_path) in ([], [execute["resource_id"]])  # delivery order decides; never twice
            assert children(process.process.pid) == []
            assert (await destroy(client, "cmd_destroy"))["status"] == "accepted"
            await vcp.stop()
    asyncio.run(scenario())


async def daemon_is(state, vcp, attempt_id):
    return await daemon_state(vcp, attempt_id) == state


async def delivered(vcp):
    return all(row["status"] == "acknowledged" for row in await commands(vcp.store))


def launches(tmp_path):
    path = tmp_path / "effects"
    return path.read_text().splitlines() if path.exists() else []
