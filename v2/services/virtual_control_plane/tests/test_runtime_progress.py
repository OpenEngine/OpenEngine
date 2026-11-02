"""Conversation and usage progress from the daemon's Codex ACP executor, projected by VCP, all in process.

The agent is the deterministic ACP subprocess fixture behind a test-only
process-group fence: fixture evidence, not an authenticated model.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from openengine.messaging import Server, envelopes

from virtual_control_plane.application.execution import Execution
from virtual_control_plane.application.lifecycle import Lifecycle
from virtual_control_plane.application.runtime_delivery import RuntimeCommands, RuntimeUpdates
from virtual_control_plane.tests.test_runtime_delivery import (
    RUNTIME, UPDATES, HostContext, Local, drain, events, execute, vcp_store, workspace_root,
)
from workspace_daemon import store as journal_store
from workspace_daemon.executor import ExecutionWorker
from workspace_daemon.initialization import locations
from workspace_daemon.journal import Journal, encode
from workspace_daemon.outbox import UpdateOutbox
from workspace_daemon.tests.acp_fixture import codex


def request(mode="new", session_id=None):
    session = {"mode": mode, **({"session_id": session_id} if session_id else {})}
    return {"agent": {"runner": "codex", "model": "fixture-large"}, "task": {"instruction": "Summarize"}, "session": session}


async def setup(tmp_path):
    root = workspace_root(tmp_path)
    store, _ = await vcp_store(tmp_path, {"path": str(root)})
    execution = Execution(Lifecycle(store, {}), HostContext(), enabled=True)
    server = Server(None, "vcp", "nats://127.0.0.1:4222", commands=execution.commands())
    updates = RuntimeUpdates(store, execution)
    directory, root = locations(tmp_path / "journal", root)
    journal_store.upgrade(journal_store.database(directory))
    journal = Journal(journal_store.JournalStore(journal_store.database(directory)), {
        "workspace_id": "ws_flow", "runtime_id": RUNTIME, "runtime_generation": 1}, root, {"codex": codex(tmp_path)})
    local = Local(journal, updates)
    dispatcher, outbox, worker = RuntimeCommands(store, local, execution), UpdateOutbox(journal, local), ExecutionWorker(journal)
    await journal.recover()
    await drain(outbox)
    return store, server, journal, updates, dispatcher, outbox, worker


async def complete(server, dispatcher, worker, outbox, body, command_id):
    reply = await execute(server, envelopes.command(body, command_id=command_id))
    assert reply["status"] == "accepted", reply
    await drain(dispatcher, worker)
    await asyncio.wait_for(asyncio.gather(*worker.tasks.values()), 30)
    await drain(outbox)
    return reply


def test_conversation_and_usage_project_atomically_once_and_continue_the_conversation(tmp_path):
    async def scenario():
        store, server, journal, updates, dispatcher, outbox, worker = await setup(tmp_path)
        assert (await store.read("runtime_status", RUNTIME))["progress_kinds"] == ["conversation", "usage"]
        first = await complete(server, dispatcher, worker, outbox, request(), "cmd_first")
        session_id = (await store.read("attempts", first["resource_id"]))["snapshot"]["session_id"]
        await complete(server, dispatcher, worker, outbox, request("continue", session_id), "cmd_second")
        published = await events(store)  # each envelope was validated against the public contract on append
        assert [e["event_type"] for e in published] == [
            "task.started", "conversation.message", "conversation.message", "usage.reported", "task.completed"] * 2
        messages = [e for e in published if e["event_type"] == "conversation.message"]
        assert [m["payload"]["sequence"] for m in messages] == [1, 2, 3, 4]
        assert all(m["resources"]["session_id"] == session_id for m in messages)
        assert "Replayed history" not in [m["payload"]["text"] for m in messages]
        usage = [e["payload"] for e in published if e["event_type"] == "usage.reported"]
        assert [u["usage"] for u in usage] == [{"input_tokens": 10, "output_tokens": 7, "cache_read_tokens": 3}] * 2
        session = (await store.read("sessions", session_id))["snapshot"]
        assert [a["usage"] for a in session["attempts"]] == [{"input_tokens": 10, "output_tokens": 7, "cache_read_tokens": 3}] * 2
        assert all("cost_usd" not in a["usage"] for a in session["attempts"])
        assert session["state"] == "idle" and [a["state"] for a in session["attempts"]] == ["completed", "completed"]
        log = [json.loads(line) for line in (tmp_path / "acp.log").read_text().splitlines()]
        assert [f["method"] for f in log if f.get("method") in ("session/new", "session/load")] == ["session/new", "session/load"]

        # Redelivery of a committed usage delta adds nothing.
        async with journal.store.read() as tx:
            wires = [row.wire for row in (await tx._connection.execute(
                sa.select(journal_store.updates.c.wire).order_by(journal_store.updates.c.sequence)))]
        delivered = [json.loads(wire) for wire in wires]
        usage_update = next(u for u in delivered if u["kind"] == "attempt.progress" and u["payload"]["kind"] == "usage")
        assert json.loads(await updates.receive(UPDATES, encode(usage_update)))["status"] == "committed"
        assert await events(store) == published
        assert await events(store) == published
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("second", ["overlapping_usage", "artifact"])
def test_overlapping_usage_and_unregistered_progress_are_conflicts(tmp_path, second):
    async def scenario():
        store, server, journal, updates, dispatcher, outbox, worker = await setup(tmp_path)
        reply = await execute(server, envelopes.command(request(), command_id="cmd_overlap"))
        await drain(dispatcher)
        artifact = {"artifact_id": "artifact_forged", "workspace_id": "ws_flow", "kind": "file", "name": "report.md",
                    "size_bytes": 1, "uri": "file:///tmp/report.md", "created_at": "2026-01-01T00:00:00Z"}
        later = {"kind": "artifact", "artifact": {**artifact, "attempt_id": reply["resource_id"]}} if second == "artifact" else {
            "kind": "usage", "period_start": "2026-01-01T00:05:00Z", "period_end": "2026-01-01T00:20:00Z",
            "usage": {"input_tokens": 1, "output_tokens": 1}}
        # Bypasses the daemon's own checks, as a faulty or forged runtime would.
        async with journal.store.transaction() as tx:
            attempt = await tx.attempt(reply["resource_id"])
            attempt.update(state="running", launch_count=1)
            await journal.append(tx, "attempt.started", {}, attempt)
            await journal.append(tx, "attempt.progress", {"kind": "usage", "period_start": "2026-01-01T00:00:00Z",
                                                          "period_end": "2026-01-01T00:10:00Z",
                                                          "usage": {"input_tokens": 1, "output_tokens": 1}}, attempt)
            await journal.append(tx, "attempt.progress", later, attempt)
            await tx.save_attempt(attempt)
        await drain(outbox)
        snapshot = (await store.read("attempts", reply["resource_id"]))["snapshot"]
        assert snapshot["usage"] == {"input_tokens": 1, "output_tokens": 1}
        status = await store.read("runtime_status", RUNTIME)
        assert status["quarantined"] and status["quarantine"]["code"] == "EVENT_CONFLICT"
        assert [e["event_type"] for e in await events(store)] == ["task.started", "usage.reported"]
        await store.close()
        await journal.store.close()
    asyncio.run(scenario())
