"""VCP delivery to the workspace daemon: in-process protocol cases, then a daemon subprocess over real NATS.

The daemon runs only the fake executor against a temporary workspace root. No
smolvm binary, cgroup setup, VM image, ACP subprocess or model is involved.
"""
from __future__ import annotations

import asyncio
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import pytest
import sqlalchemy as sa

from openengine.contracts.runtime import validate_wire_message
from openengine.messaging import Client, LocalOperator, Server, ServiceError, Unavailable, envelopes, listen_address
from openengine.persistence import DatabaseConfig
from openengine.supervisor.processes.nats import install_nats_server

from virtual_control_plane.application import runtime_delivery
from virtual_control_plane.application.execution import Execution
from virtual_control_plane.application.lifecycle import Lifecycle
from virtual_control_plane.application.runtime_delivery import RuntimeCommands, RuntimeUpdates
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore, Transaction
from virtual_control_plane.persistence.schema import runtime_commands
from virtual_control_plane.runtime_nats import NatsRuntimeTransport, RuntimeUpdateListener
from virtual_control_plane.tests.test_lifecycle import broker
from workspace_daemon import store as journal_store
from workspace_daemon.executor import ExecutionWorker, FakeExecutor
from workspace_daemon.initialization import locations
from workspace_daemon.journal import Journal, encode
from workspace_daemon.outbox import UpdateOutbox

WORKSPACE, RUNTIME = "ws_flow", "rt_flow"
BINDING = {"workspace_id": WORKSPACE, "runtime_id": RUNTIME, "runtime_generation": 1}
EXECUTE = f"oe.v1.vcp.control.workspaces.{WORKSPACE}.execute"
UPDATES = f"oe.internal.v1.vcp.runtimes.{RUNTIME}.updates"


class HostContext:
    """Worktree-shaped: the locator is a host path, which is the workspace root."""

    async def prepare(self, workspace, runtime, payload):
        return runtime["locator"]["path"], {"working_directory": ".", "instruction_files": ["AGENTS.md"], "references": []}


class GuestContext:
    """VM-shaped: the locator names a machine; a guest adapter resolves where its root is visible."""

    def __init__(self, roots):
        self.roots = roots

    async def prepare(self, workspace, runtime, payload):
        return self.roots[runtime["locator"]["vm_id"]], {"working_directory": ".", "instruction_files": ["AGENTS.md"], "references": []}


def payload(mode="new", session_id=None, **settings):
    session = {"mode": mode, **({"session_id": session_id} if session_id else {})}
    return {"agent": {"runner": "fake", "settings": {"delay_seconds": 0, **settings}}, "task": {"instruction": "Test"},
            "session": session}


async def vcp_store(tmp_path, locator):
    config = DatabaseConfig(sqlite_directory=tmp_path / "vcp")
    PERSISTENCE.upgrade(config)
    store = DurableStore(PERSISTENCE.database(config))
    async with store.transaction() as tx:
        await tx.put("workspaces", WORKSPACE, {"specification": {"apiVersion": "openengine.cc/v1alpha1"}, "snapshot": {
            "workspace_id": WORKSPACE, "state": "ready", "version": 2, "active_attempt_ids": [],
            "runtime": {"runtime_id": RUNTIME, "generation": 1, "state": "running"}}})
        await tx.put("workspace_runtimes", WORKSPACE, {"runtime_id": RUNTIME, "generation": 1, "locator": locator})
    return store, config


def workspace_root(tmp_path, name="root"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "AGENTS.md").write_text("instructions")
    return root


async def commands(store):
    async with store.database.connect() as connection:
        return list((await connection.execute(sa.select(runtime_commands))).mappings())


async def events(store):
    return [event.envelope for event in await store.pending_events(100)]


async def execute(server, body):
    return json.loads(await server.handle(EXECUTE, envelopes.encode(body)))


class Local:
    """Both protocol directions in one process, through the same validated bytes as NATS."""

    def __init__(self, journal, updates):
        self.journal, self.updates, self.sent = journal, updates, []

    async def send(self, subject, wire):
        self.sent.append((subject, wire))
        action = subject.rsplit(".", 1)[-1]
        if action == "updates":
            if (reply := await self.updates.receive(subject, wire)) is None:
                raise Unavailable(subject, "discarded")
            return reply
        assert validate_wire_message(action, wire, subject) == []
        handler = self.journal.execute if action == "execute" else self.journal.inspect
        return encode(await handler(subject, json.loads(wire)))


async def daemon(tmp_path, root, binding=BINDING):
    directory, root = locations(tmp_path / "journal", root)
    database = journal_store.database(directory)
    journal_store.upgrade(database)
    return Journal(journal_store.JournalStore(database), binding, root, {"fake": FakeExecutor(tmp_path / "effects")})


async def drain(*workers):
    """Run workers, ignoring backoff timers, until three rounds make no progress (losses are retried)."""
    idle = 0
    while idle < 3:
        progress = 0
        for worker in workers:
            if hasattr(worker, "backoff"):
                worker.backoff._pending.clear()
            if isinstance(worker, UpdateOutbox):
                while await worker.run_once():
                    progress += 1
            else:
                progress += await worker.run_once()
        idle = 0 if progress else idle + 1


async def flow(tmp_path, context, locator, root, **settings):
    """Readiness, public acceptance, delivery, execution and projection, all in process."""
    store, _ = await vcp_store(tmp_path, locator)
    execution = Execution(Lifecycle(store, {}), context, enabled=True)
    server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=execution.commands(), queries=execution.queries())
    updates = RuntimeUpdates(store, execution)
    journal = await daemon(tmp_path, root)
    local = Local(journal, updates)
    dispatcher, outbox = RuntimeCommands(store, local, execution), UpdateOutbox(journal, local)
    early = await execute(server, envelopes.command(payload(**settings), command_id="cmd_early"))
    assert early["error"]["code"] == "UNAVAILABLE"  # readiness is committed before admission
    await journal.recover()
    await drain(outbox)
    reply = await execute(server, envelopes.command(payload(**settings), command_id="cmd_flow"))
    assert reply["status"] == "accepted", reply
    worker = ExecutionWorker(journal)
    await drain(dispatcher, worker)
    await asyncio.gather(*worker.tasks.values())
    await drain(outbox)
    return store, journal, local, dispatcher, outbox, updates, reply


def normalized(value):
    text = json.dumps(value, sort_keys=True)
    text = re.sub(r'"(rtc_|attempt_|session_|op_|evt_|rte_|boot_|journal_)[0-9a-f]{32}"', r'"\1"', text)
    return json.loads(re.sub(r'"\d{4}-\d\d-\d\dT[^"]+Z"', '"T"', text))


def test_host_and_guest_placements_encode_replay_and_project_identically(tmp_path):
    async def scenario():
        results = []
        for name, context, locator in [("host", HostContext(), None), ("guest", None, {"vm_id": "vm_1"})]:
            base = tmp_path / name
            base.mkdir()
            root = workspace_root(base)
            context = context or GuestContext({"vm_1": str(root)})
            store, journal, local, _, _, _, reply = await flow(base, context, locator or {"path": str(root)}, root)
            wire = (await commands(store))[0]
            assert wire["status"] == "acknowledged"
            assert [w for s, w in local.sent if s == wire["subject"]] == [wire["wire"]]
            assert b"vm_1" not in wire["wire"] and str(root).encode() not in wire["wire"]
            session_id = json.loads(wire["wire"])["target"]["session_id"]
            session = (await store.read("sessions", session_id))["snapshot"]
            operation = (await store.read("operations", reply["operation_id"]))["snapshot"]
            results.append(normalized({"wire": json.loads(wire["wire"]), "session": session, "operation": operation,
                                       "events": [(e["event_type"], e["payload"]) for e in await events(store)]}))
            await store.close()
            await journal.store.close()
        assert results[0] == results[1]
        assert [kind for kind, _ in results[0]["events"]] == ["task.started", "task.completed"]
        assert results[0]["session"]["state"] == "idle" and results[0]["operation"]["status"] == "succeeded"
    asyncio.run(scenario())


def test_lost_acknowledgements_resend_identical_bytes_without_duplicate_effects(tmp_path):
    async def scenario():
        root = workspace_root(tmp_path)
        store, _ = await vcp_store(tmp_path, {"path": str(root)})
        execution = Execution(Lifecycle(store, {}), HostContext(), enabled=True)
        server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=execution.commands())
        updates = RuntimeUpdates(store, execution)
        journal = await daemon(tmp_path, root)
        local = Local(journal, updates)

        class Lossy:
            def __init__(self):
                self.lose = 0

            async def send(self, subject, wire):
                reply = await local.send(subject, wire)
                if self.lose:
                    self.lose -= 1
                    raise Unavailable(subject, "reply lost")
                return reply
        lossy = Lossy()
        dispatcher, outbox, worker = RuntimeCommands(store, lossy, execution), UpdateOutbox(journal, lossy), ExecutionWorker(journal)
        await journal.recover()
        lossy.lose = 1
        await drain(outbox)
        reply = await execute(server, envelopes.command(payload(), command_id="cmd_lossy"))
        lossy.lose = 1
        assert await dispatcher.run_once() == 0
        # A restarted dispatcher forgets timers, never the stored bytes.
        dispatcher = RuntimeCommands(store, lossy, execution)
        assert await dispatcher.run_once() == 1
        sent = [wire for subject, wire in local.sent if subject.endswith(".execute")]
        assert len(sent) == 2 and sent[0] == sent[1] == (await commands(store))[0]["wire"]
        await drain(worker)
        await asyncio.gather(*worker.tasks.values())
        lossy.lose = 2
        await drain(outbox)
        delivered = [wire for subject, wire in local.sent if subject == UPDATES]
        assert len(delivered) > len(set(delivered))  # duplicates were delivered ...
        assert [e["event_type"] for e in await events(store)] == ["task.started", "task.completed"]  # ... and deduplicated
        assert (await store.read("operations", reply["operation_id"]))["snapshot"]["status"] == "succeeded"
        assert (tmp_path / "effects").read_text().splitlines() == [reply["resource_id"]]
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fenced", [True, False])
def test_update_gaps_duplicates_conflicts_and_stale_bindings(tmp_path, monkeypatch, fenced):
    """`fenced=False` is the negative control: without the ownership check a stale update commits."""
    async def scenario():
        root = workspace_root(tmp_path)
        store, journal, _, _, _, updates, _ = await flow(tmp_path, HostContext(), {"path": str(root)}, root)
        async with journal.store.read() as tx:
            wires = [row.wire for row in (await tx._connection.execute(sa.select(journal_store.updates.c.wire)
                                                                        .order_by(journal_store.updates.c.sequence)))]
        messages = [json.loads(wire) for wire in wires]

        async def ack(message, subject=UPDATES):
            return json.loads(await updates.receive(subject, encode(message)))
        if not fenced:
            monkeypatch.setattr(runtime_delivery, "fence", lambda *args: None)
        stale = deepcopy(messages[0])  # runtime.ready claiming a generation VCP never provisioned
        stale["target"]["runtime_generation"] = 2
        stale["sequence"] = len(messages) + 1
        stale["event_id"] = "rte_stale"
        result = await ack(stale)
        if not fenced:
            assert result["status"] == "committed"  # the rejection test detects the missing check
            await store.close()
            await journal.store.close()
            return
        assert result["error"]["code"] == "STALE_RUNTIME"
        wrong = deepcopy(stale)
        wrong["target"].update(runtime_id="rt_other", runtime_generation=1)
        assert (await ack(wrong, UPDATES.replace(RUNTIME, "rt_other")))["error"]["code"] == "IDENTITY_MISMATCH"
        cause = deepcopy(messages[2])
        cause.update(sequence=len(messages) + 1, event_id="rte_cause")
        cause["execution"]["cause"]["operation"]["operation_id"] = "op_forged"
        assert (await ack(cause))["error"]["code"] == "IDENTITY_MISMATCH"
        gap = deepcopy(messages[1])
        gap.update(sequence=len(messages) + 2, event_id="rte_gap")
        assert (await ack(gap)) | {"error": None} == {"event_id": "rte_gap", "target": BINDING, "sequence": len(messages) + 2,
                                                        "status": "rejected", "expected_sequence": len(messages) + 1, "error": None}
        before = await events(store)
        assert (await ack(messages[1]))["status"] == "committed"  # duplicate: no second effect
        assert await events(store) == before
        changed = deepcopy(messages[1])
        changed["boot_id"] = "boot_changed"
        assert (await ack(changed))["error"]["code"] == "EVENT_CONFLICT"
        status = await store.read("runtime_status", RUNTIME)
        assert status["quarantined"] and not status["ready"] and status["quarantine"]["event_id"] == messages[1]["event_id"]
        assert status["sequence"] == len(messages)
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


def test_update_projection_rolls_back_as_one_transaction(tmp_path, monkeypatch):
    async def scenario():
        root = workspace_root(tmp_path)
        store, _ = await vcp_store(tmp_path, {"path": str(root)})
        execution = Execution(Lifecycle(store, {}), HostContext(), enabled=True)
        server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=execution.commands())
        updates = RuntimeUpdates(store, execution)
        journal = await daemon(tmp_path, root)
        local = Local(journal, updates)
        dispatcher, outbox, worker = RuntimeCommands(store, local, execution), UpdateOutbox(journal, local), ExecutionWorker(journal)
        await journal.recover()
        await drain(outbox)
        reply = await execute(server, envelopes.command(payload(), command_id="cmd_rollback"))
        await drain(dispatcher, worker)
        await asyncio.gather(*worker.tasks.values())
        original = Transaction.append_event

        async def fail(self, envelope):
            await original(self, envelope)
            raise RuntimeError()  # storage failed after the event write
        monkeypatch.setattr(Transaction, "append_event", fail)
        outbox.backoff._pending.clear()
        with pytest.raises(RuntimeError):
            await outbox.run_once()
        assert (await store.read("runtime_status", RUNTIME))["sequence"] == 1
        assert (await store.read("attempts", reply["resource_id"]))["snapshot"]["state"] == "starting"
        assert (await store.read("operations", reply["operation_id"]))["snapshot"]["status"] == "pending"
        assert await events(store) == []
        monkeypatch.setattr(Transaction, "append_event", original)
        await drain(outbox)
        assert [e["event_type"] for e in await events(store)] == ["task.started", "task.completed"]
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("rejection", ["context", "stale"])
def test_daemon_rejections_fail_unstarted_work_or_block_unresolved_ownership(tmp_path, rejection):
    async def scenario():
        vcp_root, daemon_root = workspace_root(tmp_path, "vcp-root"), workspace_root(tmp_path, "daemon-root")
        if rejection == "context":
            (daemon_root / "AGENTS.md").unlink()  # the daemon's own view rejects the context
        store, _ = await vcp_store(tmp_path, {"path": str(vcp_root)})
        execution = Execution(Lifecycle(store, {}), HostContext(), enabled=True)
        server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=execution.commands())
        updates = RuntimeUpdates(store, execution)
        journal = await daemon(tmp_path, daemon_root)
        local = Local(journal, updates)
        await journal.recover()
        await drain(UpdateOutbox(journal, local))
        reply = await execute(server, envelopes.command(payload(), command_id="cmd_rejected"))
        if rejection == "stale":
            journal.binding["runtime_generation"] = 2  # a daemon provisioned for another generation
        await RuntimeCommands(store, local, execution).run_once()
        row = (await commands(store))[0]
        attempt = (await store.read("attempts", reply["resource_id"]))["snapshot"]
        workspace = (await store.read("workspaces", WORKSPACE))["snapshot"]
        acknowledgement = json.loads(row["acknowledgement"])
        if rejection == "context":
            assert (row["status"], acknowledgement["error"]["code"]) == ("rejected", "INVALID_ARGUMENT")
            assert attempt["state"] == "failed" and workspace["active_attempt_ids"] == []
            assert (await store.read("operations", reply["operation_id"]))["snapshot"]["status"] == "failed"
        else:
            # Not evidence that no executor exists: ownership stays unresolved.
            assert (row["status"], acknowledgement["error"]["code"]) == ("blocked", "STALE_RUNTIME")
            assert attempt["state"] == "starting" and workspace["active_attempt_ids"] == [reply["resource_id"]]
        assert await RuntimeCommands(store, local, execution).run_once() == 0
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


def test_committed_updates_stay_committed_after_the_workspace_leaves_ready(tmp_path):
    async def scenario():
        root = workspace_root(tmp_path)
        store, journal, _, _, _, updates, _ = await flow(tmp_path, HostContext(), {"path": str(root)}, root)
        async with journal.store.read() as tx:
            wires = [row.wire for row in (await tx._connection.execute(sa.select(journal_store.updates.c.wire)
                                                                        .order_by(journal_store.updates.c.sequence)))]
        async with store.transaction() as tx:
            record = await tx.get("workspaces", WORKSPACE)
            record["snapshot"]["state"] = "failed"  # the runtime binding is still current
            await tx.put("workspaces", WORKSPACE, record)
        for wire in wires:
            assert json.loads(await updates.receive(UPDATES, wire))["status"] == "committed"  # re-sends, no refusal
        ready = json.loads(wires[0])
        ready.update(event_id="rte_ready_again", sequence=len(wires) + 1)
        error = json.loads(await updates.receive(UPDATES, encode(ready)))["error"]
        assert (error["code"], error["retryable"]) == ("UNAVAILABLE", True)  # waits instead of blocking the daemon
        assert not (await store.read("runtime_status", RUNTIME)).get("quarantined")
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


def test_an_unreachable_runtime_does_not_hold_up_other_runtimes(tmp_path):
    async def scenario():
        root = workspace_root(tmp_path)
        store, _ = await vcp_store(tmp_path, {"path": str(root)})
        execution = Execution(Lifecycle(store, {}), HostContext(), enabled=True)
        server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=execution.commands())
        journal = await daemon(tmp_path, root)
        local = Local(journal, RuntimeUpdates(store, execution))
        await journal.recover()
        await drain(UpdateOutbox(journal, local))
        dead = "oe.internal.v1.runtime.runtimes.rt_dead.execute"
        async with store.transaction() as tx:
            for n in range(20):
                await tx.put("attempts", f"attempt_dead_{n}", {})
                await tx.append_runtime_command(f"rtc_dead_{n}", f"attempt_dead_{n}", dead, b"{}")
        reply = await execute(server, envelopes.command(payload(), command_id="cmd_healthy"))

        class Partitioned:
            dead_sends = 0

            async def send(self, subject, wire):
                if subject == dead:
                    self.dead_sends += 1
                    raise Unavailable(subject, "no reply")
                return await local.send(subject, wire)
        transport = Partitioned()
        dispatcher = RuntimeCommands(store, transport, execution, batch=16)
        assert await dispatcher.run_once() == 0  # a full batch for the dead runtime, probed once
        assert await dispatcher.run_once() == 1  # backing-off commands no longer fill the batch
        assert transport.dead_sends == 2
        row = next(row for row in await commands(store) if row["subject"] != dead)
        assert row["status"] == "acknowledged" and json.loads(row["wire"])["target"]["attempt_id"] == reply["resource_id"]
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


def test_ready_during_provisioning_is_retried_not_committed(tmp_path):
    async def scenario():
        root = workspace_root(tmp_path)
        store, _ = await vcp_store(tmp_path, {"path": str(root)})
        async with store.transaction() as tx:
            record = await tx.get("workspaces", WORKSPACE)
            record["snapshot"]["state"] = "provisioning"
            del record["snapshot"]["runtime"]
            await tx.put("workspaces", WORKSPACE, record)
        execution = Execution(Lifecycle(store, {}), HostContext(), enabled=True)
        journal = await daemon(tmp_path, root)
        await journal.recover()
        outbox = UpdateOutbox(journal, Local(journal, RuntimeUpdates(store, execution)))
        assert not await outbox.run_once()
        assert await store.read("runtime_status", RUNTIME) is None
        async with journal.store.read() as tx:
            meta = await tx.runtime()
            assert not meta.get("blocked") and await tx.oldest_pending() is not None
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


# Real NATS, with the daemon as an ordinary host subprocess.


@pytest.fixture(scope="module")
def binary():
    return install_nats_server(Path(tempfile.gettempdir()) / "oe-test-nats")


async def until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := await predicate():
            return result
        await asyncio.sleep(0.05)
    pytest.fail("condition not reached")


class Daemon:
    def __init__(self, tmp_path, url, root):
        self.tmp_path, self.url, self.root = tmp_path, url, root
        self.process = None

    async def start(self, initialize=False):
        ready = self.tmp_path / "daemon.ready"
        args = [sys.executable, "-m", "workspace_daemon", "--nats-url", self.url, "--journal-directory",
                str(self.tmp_path / "journal"), "--workspace-root", str(self.root), "--workspace-id", WORKSPACE,
                "--runtime-id", RUNTIME, "--generation", "1", "--local-test-account", "--ready-file", str(ready),
                "--fake-effects", str(self.tmp_path / "effects")] + (["--initialize"] if initialize else [])
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        async def listening():
            assert self.process.poll() is None, "daemon exited"
            return ready.exists()
        await until(listening)

    def stop(self, sig=signal.SIGTERM):
        if self.process and self.process.poll() is None:
            self.process.send_signal(sig)
            self.process.wait(timeout=10)


class Vcp:
    def __init__(self, connection, config, authorizer=None):
        self.store = DurableStore(PERSISTENCE.database(config))
        self.execution = Execution(Lifecycle(self.store, {}), HostContext(), enabled=True)
        self.server = Server(connection, "vcp", listen_address(connection), commands=self.execution.commands(),
                             queries=self.execution.queries(), authorizer=authorizer or LocalOperator())
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
        url = connection.connected_url.geturl()
        process = Daemon(tmp_path, url, root)
        try:
            yield connection, config, process
        finally:
            process.stop(signal.SIGKILL)


async def ready(vcp):
    return (status := await vcp.store.read("runtime_status", RUNTIME)) and status.get("ready")


async def settled(vcp, attempt_id, state):
    return (await vcp.store.read("attempts", attempt_id))["snapshot"]["state"] == state


def launches(tmp_path):
    path = tmp_path / "effects"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_daemon_subprocess_completes_fake_tasks_over_real_nats(binary, tmp_path, outcome):
    async def scenario():
        async with deployment(binary, tmp_path) as (connection, config, process):
            vcp = await Vcp(connection, config).start()
            await process.start(initialize=True)
            await until(lambda: ready(vcp))
            client = Client(connection)
            reply = await client.command(EXECUTE, payload(outcome=outcome, delay_seconds=1.5), command_id="cmd_real")
            await until(lambda: settled(vcp, reply["resource_id"], "running"))
            started = time.monotonic()
            inspect = await vcp.dispatcher.inspect({"runtime_id": RUNTIME, "generation": 1}, WORKSPACE, [reply["resource_id"]])
            assert time.monotonic() - started < 1.0  # inspect stays responsive while the task runs
            assert inspect["result"]["attempts"][0]["state"] == "running" and inspect["result"]["ready"]
            await until(lambda: settled(vcp, reply["resource_id"], outcome))
            session_id = json.loads((await commands(vcp.store))[0]["wire"])["target"]["session_id"]
            session = (await client.query(f"oe.v1.vcp.query.sessions.{session_id}.get"))["result"]["session"]
            assert session["state"] == "idle" and session["attempts"][0]["state"] == outcome
            assert (await vcp.store.read("operations", reply["operation_id"]))["snapshot"]["status"] == "succeeded"
            assert [e["event_type"] for e in await events(vcp.store)] == ["task.started", f"task.{outcome}"]
            assert launches(tmp_path) == [reply["resource_id"]]
            await vcp.stop()
    asyncio.run(scenario())


def test_vcp_and_daemon_restarts_replay_without_duplicate_launch(binary, tmp_path):
    async def scenario():
        async with deployment(binary, tmp_path) as (connection, config, process):
            vcp = await Vcp(connection, config).start(deliver=False)
            await process.start(initialize=True)
            await until(lambda: ready(vcp))
            client = Client(connection)
            first = await client.command(EXECUTE, payload(delay_seconds=1.0), command_id="cmd_first")
            # VCP stops after acceptance, before delivery; a restart sends the stored bytes.
            await vcp.stop()
            vcp = await Vcp(connection, config).start()
            await until(lambda: settled(vcp, first["resource_id"], "running"))
            # VCP stops while the task runs; the daemon keeps its updates until VCP returns.
            await vcp.stop()
            await asyncio.sleep(1.5)
            vcp = await Vcp(connection, config).start()
            await until(lambda: settled(vcp, first["resource_id"], "completed"))
            second = await client.command(EXECUTE, payload(delay_seconds=30), command_id="cmd_second")
            await until(lambda: settled(vcp, second["resource_id"], "running"))
            # The daemon dies mid-task. Its restart fences the uncertain launch instead of rerunning it.
            process.stop(signal.SIGKILL)
            await process.start()
            await until(lambda: settled(vcp, second["resource_id"], "failed"))
            attempt = (await vcp.store.read("attempts", second["resource_id"]))["snapshot"]
            assert attempt["error"]["code"] == "PROVIDER_ERROR"
            assert (await vcp.store.read("operations", second["operation_id"]))["snapshot"]["status"] == "succeeded"
            assert launches(tmp_path) == [first["resource_id"], second["resource_id"]]
            # Replaying public execute returns the original acceptance; nothing is enqueued or launched.
            assert await client.command(EXECUTE, payload(delay_seconds=1.0), command_id="cmd_first") == first
            assert len(await commands(vcp.store)) == 2
            status = await vcp.store.read("runtime_status", RUNTIME)
            assert status["ready"] and not status.get("quarantined")
            session_id = json.loads((await commands(vcp.store))[0]["wire"])["target"]["session_id"]
            third = await client.command(EXECUTE, payload("continue", session_id, delay_seconds=1.0), command_id="cmd_third")
            await until(lambda: settled(vcp, third["resource_id"], "completed"))
            assert launches(tmp_path)[-1] == third["resource_id"] and len(launches(tmp_path)) == 3
            await vcp.stop()
    asyncio.run(scenario())


def test_denied_public_request_and_forged_internal_messages_have_no_effect(binary, tmp_path):
    class Deny:
        def authorize(self, request):
            raise ServiceError("PERMISSION_DENIED", "denied", details={"permission": "control.workspaces.execute"})

        def authorize_project(self, request, project):
            self.authorize(request)

    async def scenario():
        async with deployment(binary, tmp_path) as (connection, config, process):
            vcp = await Vcp(connection, config, Deny()).start()
            await process.start(initialize=True)
            await until(lambda: ready(vcp))
            with pytest.raises(ServiceError) as denied:
                await Client(connection).command(EXECUTE, payload(), command_id="cmd_denied")
            assert denied.value.code == "PERMISSION_DENIED"
            async with vcp.store.transaction() as tx:
                assert await tx.all("sessions") == await tx.all("attempts") == []
            assert await commands(vcp.store) == []
            # A forged internal execute cannot bypass the denial with a wrong binding.
            forged = {"command_id": "rtc_forged", "kind": "execute",
                      "target": {**BINDING, "runtime_generation": 2, "session_id": "session_forged", "attempt_id": "attempt_forged"},
                      "execution": {"command_id": "rtc_forged", "cause": {"operation": {"operation_id": "op_forged",
                                    "command_id": "cmd_denied"}, "causal_path": ["cmd_denied"]}},
                      "payload": {"agent": {"runner": "fake"}, "task": {"instruction": "x"}, "session_mode": "new",
                                  "context": {"working_directory": ".", "instruction_files": [], "references": []}}}
            subject = f"oe.internal.v1.runtime.runtimes.{RUNTIME}.execute"
            ack = json.loads((await connection.request(subject, encode(forged), timeout=5)).data)
            assert ack["error"]["code"] == "STALE_RUNTIME"
            inspect = await vcp.dispatcher.inspect({"runtime_id": RUNTIME, "generation": 1}, WORKSPACE, ["attempt_forged"])
            assert inspect["result"]["attempts"] == [{"attempt_id": "attempt_forged", "known": False}]
            # A forged update for an unknown attempt is rejected without creating state.
            update = {"event_id": "rte_forged", "kind": "attempt.started",
                      "target": {**BINDING, "session_id": "session_forged", "attempt_id": "attempt_forged"},
                      "boot_id": "boot_forged", "sequence": 2, "occurred_at": "2026-01-01T00:00:00Z", "payload": {},
                      "execution": forged["execution"], "attempt_sequence": 1}
            reply = json.loads((await connection.request(UPDATES, encode(update), timeout=5)).data)
            assert reply["error"]["code"] == "IDENTITY_MISMATCH"
            assert await events(vcp.store) == [] and launches(tmp_path) == []
            await vcp.stop()
    asyncio.run(scenario())
