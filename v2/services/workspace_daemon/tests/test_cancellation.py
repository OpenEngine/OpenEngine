"""Daemon cancellation: journaled intent, tombstones, races and confirmed termination; no broker or VM.

The Codex cases run the deterministic ACP fixture under `GroupFence`, which
kills a POSIX process group: fixture evidence, not cgroup-v2 fencing or an
authenticated model.
"""
from __future__ import annotations

import asyncio
import os
import time
from copy import deepcopy

import pytest

from openengine.contracts.runtime import validate_wire_message

from workspace_daemon import store as journal_store
from workspace_daemon.executor import ExecutionWorker, FakeExecutor
from workspace_daemon.journal import Journal
from workspace_daemon.tests.acp_fixture import GroupFence, codex
from workspace_daemon.tests.test_codex import frames, state, task, updates
from workspace_daemon.tests.test_codex import setup as codex_setup
from workspace_daemon.tests.test_journal import BINDING_TARGET, EXECUTE, command, drain, launches, setup

CANCEL = EXECUTE.replace(".execute", ".cancel")


def cancel(execute, command_id="rtc_cancel", reason="User cancelled"):
    return {"command_id": command_id, "kind": "cancel", "target": deepcopy(execute["target"]),
            "execution": deepcopy(execute["execution"]),
            "cause": {"operation": {"operation_id": "op_" + command_id, "command_id": "cmd_" + command_id},
                      "causal_path": ["cmd_" + command_id]},
            "payload": {"reason": reason}}


async def settle(worker, attempt_id, journal, timeout=20.0):
    """Run the worker, ignoring backoff, until the attempt is terminal."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker.backoff._pending.clear()
        await worker.run_once()
        if (await state(journal, attempt_id))["state"] in ("completed", "failed", "cancelled"):
            await asyncio.gather(*worker.tasks.values(), *worker.stopping.values(), return_exceptions=True)
            return await state(journal, attempt_id)
        await asyncio.sleep(0.02)
    pytest.fail("attempt did not settle")


async def running(journal, attempt_id="attempt_1"):
    for _ in range(1000):
        if (await state(journal, attempt_id))["state"] == "running":
            return
        await asyncio.sleep(0.01)
    pytest.fail("attempt did not start")


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_cancel_before_execute_journals_a_tombstone_that_late_execute_never_launches(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        execute, request = command(), cancel(command())
        reply = await journal.cancel(CANCEL, request)
        assert reply == {"command_id": "rtc_cancel", "target": execute["target"], "status": "accepted"}
        assert await journal.cancel(CANCEL, request) == reply  # a lost acknowledgement replays the receipt
        changed = deepcopy(request)
        changed["payload"]["reason"] = "Changed"
        assert (await journal.cancel(CANCEL, changed))["error"]["code"] == "COMMAND_ID_CONFLICT"
        assert (await journal.cancel(CANCEL, cancel(execute, "rtc_again")))["status"] == "accepted"  # no-op
        # The delayed matching execute is acknowledged, durably, and never launched.
        assert (await journal.execute(EXECUTE, execute))["status"] == "accepted"
        assert (await journal.execute(EXECUTE, execute))["status"] == "accepted"
        other = command(command_id="rtc_other")
        assert (await journal.execute(EXECUTE, other))["error"]["code"] == "ATTEMPT_CONFLICT"
        worker = ExecutionWorker(journal)
        assert await worker.run_once() == 0
        await drain(outbox)
        cancelled = updates(vcp, "attempt.cancelled")
        assert len(cancelled) == 1 and len(updates(vcp)) == 2
        assert (cancelled[0]["termination"], cancelled[0]["cancel_command_id"], cancelled[0]["attempt_sequence"]) == (
            "never_started", "rtc_cancel", 1)
        assert cancelled[0]["cause"] == request["cause"] and cancelled[0]["execution"] == execute["execution"]
        assert cancelled[0]["payload"] == {"reason": "User cancelled"}
        for wire in vcp.committed:
            assert validate_wire_message("update", wire, journal.updates) == []
        assert launches(tmp_path) == []
        inspect = await journal.inspect(EXECUTE.replace(".execute", ".inspect"),
                                        {"request_id": "req_1", "target": BINDING_TARGET, "attempt_ids": ["attempt_1"]})
        assert inspect["result"]["attempts"][0]["state"] == "cancelled"
        # The tombstone never opened the session: a new execute may open it.
        fresh = command("attempt_2", "rtc_2")
        assert (await journal.execute(EXECUTE, fresh))["status"] == "accepted"
        await journal.store.close()
    asyncio.run(scenario())


def test_cancel_of_a_recorded_attempt_ends_it_before_launch(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        assert (await journal.execute(EXECUTE, command()))["status"] == "accepted"
        assert (await journal.cancel(CANCEL, cancel(command())))["status"] == "accepted"
        worker = ExecutionWorker(journal)
        assert await worker.run_once() == 0
        assert (await state(journal))["state"] == "cancelled" and launches(tmp_path) == []
        await drain(outbox)
        assert [u["kind"] for u in updates(vcp)] == ["runtime.ready", "attempt.cancelled"]
        assert updates(vcp, "attempt.cancelled")[0]["termination"] == "never_started"
        # The session is free again.
        assert (await journal.execute(EXECUTE, command("attempt_2", "rtc_2", mode="continue")))["status"] == "accepted"
        await journal.store.close()
    asyncio.run(scenario())


def test_cancel_conflicting_with_the_journaled_execution_is_rejected_without_effect(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        assert (await journal.execute(EXECUTE, command(delay_seconds=30)))["status"] == "accepted"
        forged = cancel(command(command_id="rtc_other"))
        assert (await journal.cancel(CANCEL, forged))["error"]["code"] == "ATTEMPT_CONFLICT"
        wrong = cancel(command())
        wrong["target"]["runtime_generation"] = 2
        assert (await journal.cancel(CANCEL, wrong))["error"]["code"] == "STALE_RUNTIME"
        assert (await state(journal))["state"] == "recorded" and (await state(journal))["cancel"] is None
        await journal.store.close()
    asyncio.run(scenario())


def test_completion_that_won_keeps_its_outcome(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        assert (await journal.execute(EXECUTE, command()))["status"] == "accepted"
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await asyncio.gather(*worker.tasks.values())
        assert (await journal.cancel(CANCEL, cancel(command())))["status"] == "accepted"
        await drain(outbox)
        assert [u["kind"] for u in updates(vcp)] == ["runtime.ready", "attempt.started", "attempt.completed"]
        assert (await state(journal))["state"] == "completed"
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("graceful", [True, False])
def test_running_fake_attempt_is_cancelled_gracefully_or_at_the_deadline(tmp_path, graceful):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        execute = command(delay_seconds=30, **({} if graceful else {"ignore_cancel": True}))
        assert (await journal.execute(EXECUTE, execute))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=0.3)
        await worker.run_once()
        await running(journal)
        # Completion and cancellation serialize: a completion after the intent is journaled reports cancelled.
        assert (await journal.cancel(CANCEL, cancel(execute)))["status"] == "accepted"
        assert (await state(journal))["state"] == "cancelling"
        busy = await journal.execute(EXECUTE, command("attempt_2", "rtc_2", mode="continue"))
        assert busy["error"]["code"] == "SESSION_BUSY"  # ownership is kept while cancelling
        started = time.monotonic()
        attempt = await settle(worker, "attempt_1", journal)
        assert attempt["state"] == "cancelled" and time.monotonic() - started < 5
        await drain(outbox)
        cancelled = updates(vcp, "attempt.cancelled")
        assert len(cancelled) == 1 and not updates(vcp, "attempt.completed") and not updates(vcp, "attempt.failed")
        # Graceful work ended on its own; otherwise its task was cancelled and the attempt then fenced.
        assert cancelled[0]["termination"] == ("exited" if graceful else "killed")
        assert launches(tmp_path) == ["attempt_1"]
        await journal.store.close()
    asyncio.run(scenario())


def test_cancel_while_launching_keeps_cancelling_through_started(tmp_path):
    class Slow(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()

        async def launch(self, *args):
            await self.release.wait()
            return await super().launch(*args)

    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        executor = Slow()
        journal.executors = {"fake": executor}
        execute = command(delay_seconds=30)
        assert (await journal.execute(EXECUTE, execute))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=5)
        await worker.run_once()
        await asyncio.sleep(0.05)
        assert (await state(journal))["state"] == "launching"
        assert (await journal.cancel(CANCEL, cancel(execute)))["status"] == "accepted"
        await worker.run_once()  # graceful stop is requested while the launch is still in progress
        executor.release.set()
        attempt = await settle(worker, "attempt_1", journal)
        assert (attempt["state"], attempt["launch_count"]) == ("cancelled", 1)
        await drain(outbox)
        assert [u["kind"] for u in updates(vcp)] == ["runtime.ready", "attempt.started", "attempt.cancelled"]
        await journal.store.close()
    asyncio.run(scenario())


def test_graceful_acp_cancel_then_the_whole_process_tree_is_fenced(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await codex_setup(tmp_path, codex(tmp_path, "cancellable", fence))
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=5)
        await worker.run_once()
        await running(journal)
        for _ in range(500):
            if (tmp_path / "acp.child").exists():
                break
            await asyncio.sleep(0.01)
        child = int((tmp_path / "acp.child").read_text())
        assert (await journal.cancel(CANCEL, cancel(task())))["status"] == "accepted"
        started = time.monotonic()
        attempt = await settle(worker, "attempt_1", journal)
        assert attempt["state"] == "cancelled" and time.monotonic() - started < 5  # ended by the agent, not the deadline
        assert [f.get("method") for f in frames(tmp_path)].count("session/cancel") == 1
        assert fence.fenced and all(p.returncode is not None for p in fence.processes.values())
        for _ in range(100):
            if gone(child):
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the agent's child outlived fencing")
        await drain(outbox)
        assert [u["payload"]["text"] for u in updates(vcp, "attempt.progress")] == ["Partial answer"]
        assert len(updates(vcp, "attempt.cancelled")) == 1 and not updates(vcp, "attempt.failed")
        await journal.store.close()
    asyncio.run(scenario())


def test_blocked_acp_stdin_cannot_defeat_the_cancellation_deadline(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await codex_setup(tmp_path, codex(tmp_path, "stop_reading", fence))
        # The agent stops reading before the prompt, which is larger than any pipe buffer: the write blocks.
        value = task(instruction="x" * 400000)
        assert (await journal.execute(EXECUTE, value))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=0.5)
        await worker.run_once()
        await running(journal)
        await asyncio.sleep(0.2)
        assert (await journal.cancel(CANCEL, cancel(value)))["status"] == "accepted"
        started = time.monotonic()
        attempt = await settle(worker, "attempt_1", journal)
        assert attempt["state"] == "cancelled" and time.monotonic() - started < 10
        assert fence.fenced and all(p.returncode is not None for p in fence.processes.values())
        await drain(outbox)
        assert updates(vcp, "attempt.cancelled")[0]["termination"] == "killed"
        await journal.store.close()
    asyncio.run(scenario())


def test_cancel_during_acp_launch_never_prompts(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await codex_setup(tmp_path, codex(tmp_path, "slow_start", fence))
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=5)
        await worker.run_once()
        await asyncio.sleep(0.2)
        assert (await state(journal))["state"] == "launching"
        assert (await journal.cancel(CANCEL, cancel(task())))["status"] == "accepted"
        attempt = await settle(worker, "attempt_1", journal)
        assert attempt["state"] == "cancelled"
        assert "session/prompt" not in [f.get("method") for f in frames(tmp_path)]
        assert all(p.returncode is not None for p in fence.processes.values())
        await drain(outbox)
        assert [u["kind"] for u in updates(vcp)] == ["runtime.ready", "attempt.started", "attempt.cancelled"]
        await journal.store.close()
    asyncio.run(scenario())


def test_full_progress_outbox_cannot_prevent_cancellation(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await codex_setup(tmp_path, codex(tmp_path, "flood", fence), max_pending_updates=2)
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=0.3)
        await worker.run_once()
        await running(journal)
        await asyncio.sleep(0.5)  # the producer now waits for outbox capacity that never comes
        assert (await journal.cancel(CANCEL, cancel(task())))["status"] == "accepted"
        attempt = await settle(worker, "attempt_1", journal)
        assert attempt["state"] == "cancelled"  # terminal records never wait for capacity
        assert all(p.returncode is not None for p in fence.processes.values())
        await drain(outbox)
        assert updates(vcp)[-1]["kind"] == "attempt.cancelled"
        await journal.store.close()
    asyncio.run(scenario())


def test_failed_fencing_keeps_cancelling_ownership_until_confirmed(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await codex_setup(tmp_path, codex(tmp_path, "hang", fence))
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=0.2)
        await worker.run_once()
        await running(journal)
        fence.confirm = False
        assert (await journal.cancel(CANCEL, cancel(task())))["status"] == "accepted"
        for _ in range(10):
            worker.backoff._pending.clear()
            await worker.run_once()
            await asyncio.sleep(0.1)
        await asyncio.gather(*worker.tasks.values(), *worker.stopping.values(), return_exceptions=True)
        assert (await state(journal))["state"] == "cancelling" and len(fence.fenced) >= 2
        busy = await journal.execute(EXECUTE, task("attempt_2", "rtc_2", mode="continue"))
        assert busy["error"]["code"] == "SESSION_BUSY"
        await drain(outbox)
        assert not updates(vcp, "attempt.cancelled")  # nothing may unlock destroy yet
        fence.confirm = True
        attempt = await settle(worker, "attempt_1", journal)
        assert (attempt["state"], attempt["launch_count"]) == ("cancelled", 1)
        assert [f.get("method") for f in frames(tmp_path)].count("session/prompt") == 1  # never re-executed
        await drain(outbox)
        assert len(updates(vcp, "attempt.cancelled")) == 1
        await journal.store.close()
    asyncio.run(scenario())


def test_failed_terminal_write_is_retried_while_ownership_is_kept(tmp_path, monkeypatch):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        execute = command(delay_seconds=30)
        assert (await journal.execute(EXECUTE, execute))["status"] == "accepted"
        worker = ExecutionWorker(journal, cancel_seconds=0.3)
        await worker.run_once()
        await running(journal)
        finish, failures = journal.finish, []

        async def lost(tx, attempt, state_, payload, termination):
            if not failures:
                failures.append(state_)
                raise OSError(28)  # ENOSPC while journaling the outcome
            await finish(tx, attempt, state_, payload, termination)
        monkeypatch.setattr(journal, "finish", lost)
        assert (await journal.cancel(CANCEL, cancel(execute)))["status"] == "accepted"
        attempt = await settle(worker, "attempt_1", journal)
        assert failures and attempt["state"] == "cancelled"
        await drain(outbox)
        assert len(updates(vcp, "attempt.cancelled")) == 1 and launches(tmp_path) == ["attempt_1"]
        await journal.store.close()
    asyncio.run(scenario())


def test_restart_recovers_the_cancel_intent_and_fences_without_relaunch(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await codex_setup(tmp_path, codex(tmp_path, "hang", fence))
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await running(journal)
        assert (await journal.cancel(CANCEL, cancel(task())))["status"] == "accepted"
        # The daemon "crashes" before stopping the work: a new process recovers from the journal alone.
        restarted = Journal(journal.store, BINDING_TARGET, journal.root, {"codex": codex(tmp_path, "hang", fence)},
                            established=True)
        evidence = await restarted.recover()
        assert [e["decision"] for e in evidence] == ["fenced_cancelled"]
        assert all(p.returncode is not None for p in fence.processes.values())
        for value in worker.tasks.values():
            value.cancel()
        await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
        outbox.journal = restarted
        await drain(outbox)
        cancelled = updates(vcp, "attempt.cancelled")
        assert len(cancelled) == 1 and cancelled[0]["termination"] == "killed"
        assert cancelled[0]["cancel_command_id"] == "rtc_cancel"
        assert [f.get("method") for f in frames(tmp_path)].count("session/prompt") == 1
        assert (await restarted.cancel(CANCEL, cancel(task())))["status"] == "accepted"  # replayed receipt
        await journal.store.close()
    asyncio.run(scenario())


def test_cancel_receipts_and_updates_survive_a_journal_reopen(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        reply = await journal.cancel(CANCEL, cancel(command()))
        await journal.store.close()
        store = journal_store.JournalStore(journal_store.database(tmp_path / "journal"))
        restarted = Journal(store, BINDING_TARGET, journal.root, journal.executors, established=True)
        await restarted.recover()
        outbox.journal = restarted
        await drain(outbox)
        assert await restarted.cancel(CANCEL, cancel(command())) == reply
        assert (await restarted.execute(EXECUTE, command()))["status"] == "accepted"
        assert await ExecutionWorker(restarted).run_once() == 0 and launches(tmp_path) == []
        assert (await state(restarted))["state"] == "cancelled"
        assert [u["kind"] for u in updates(vcp)] == ["runtime.ready", "attempt.cancelled", "runtime.ready"]
        await restarted.store.close()
    asyncio.run(scenario())
