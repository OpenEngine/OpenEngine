"""Journaled fake steering effects share the receipt and update transaction."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager

import pytest

from workspace_daemon.executor import ExecutionWorker
from workspace_daemon.store import Transaction
from workspace_daemon.tests.test_cancellation import cancel, running
from workspace_daemon.tests.test_codex import state, updates
from workspace_daemon.tests.test_journal import EXECUTE, command, drain, setup

STEER = EXECUTE.replace(".execute", ".steer")


def steer(execute, command_id="rtc_steer", message="Use the second option"):
    request = cancel(execute, command_id)
    request.update(kind="steer", payload={"message": message})
    return request


@pytest.mark.parametrize("fail_at", ["update", "attempt"])
def test_effect_receipt_and_update_roll_back_and_retry_exactly_once(tmp_path, monkeypatch, fail_at):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        execute = command(delay_seconds=30)
        await journal.execute(EXECUTE, execute)
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await running(journal)
        request = steer(execute)
        method = "append_update" if fail_at == "update" else "save_attempt"
        original = getattr(Transaction, method)
        async def fail(*args):
            raise RuntimeError("disk failure")  # noqa: TRY003 -- injected storage fault
        monkeypatch.setattr(Transaction, method, fail)
        with pytest.raises(RuntimeError, match="disk failure"):
            await journal.steer(STEER, request)
        async with journal.store.read() as tx:
            assert await tx.receipt(request["command_id"]) is None
            assert "steering" not in (await tx.attempt("attempt_1"))["executor_handle"]
        monkeypatch.setattr(Transaction, method, original)
        reply = await journal.steer(STEER, request)
        assert reply["status"] == "accepted" and await journal.steer(STEER, request) == reply
        changed = deepcopy(request)
        changed["payload"]["message"] = "Changed"
        assert (await journal.steer(STEER, changed))["error"]["code"] == "COMMAND_ID_CONFLICT"
        await drain(outbox)
        assert len(updates(vcp, "steering.applied")) == 1
        assert (await state(journal, "attempt_1"))["executor_handle"]["steering"] == [
            {"command_id": "rtc_steer", "message": "Use the second option"}]
        await worker.close()
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change,code", [("generation", "STALE_RUNTIME"), ("session", "ATTEMPT_CONFLICT"),
                                         ("execution", "ATTEMPT_CONFLICT"), ("unsupported", "UNSUPPORTED_CAPABILITY")])
def test_rejects_unsupported_and_stale_bindings_without_effects(tmp_path, change, code):
    async def scenario():
        journal, _, _ = await setup(tmp_path)
        execute = command(delay_seconds=30)
        await journal.execute(EXECUTE, execute)
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await running(journal)
        request = steer(execute)
        if change == "generation":
            request["target"]["runtime_generation"] += 1
        elif change == "session":
            request["target"]["session_id"] = "session_other"
        elif change == "execution":
            request["execution"]["command_id"] = "rtc_other"
        else:
            journal.executors["fake"].steering = False
        assert (await journal.steer(STEER, request))["error"]["code"] == code
        assert "steering" not in (await state(journal, "attempt_1"))["executor_handle"]
        await worker.close()
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["apply", "complete", "cancel"])
def test_concurrent_terminal_application_serializes_and_late_delivery_is_final(tmp_path, first, monkeypatch):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        execute = command(delay_seconds=30)
        await journal.execute(EXECUTE, execute)
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await running(journal)
        async def complete():
            async with journal.store.transaction() as tx:
                await journal.finish(tx, await tx.attempt("attempt_1"), "completed", {"result": {"summary": "Done"}}, "exited")
        async def cancellation():
            await journal.cancel(EXECUTE.replace(".execute", ".cancel"), cancel(execute))
            await complete()
        async def apply():
            return await journal.steer(STEER, steer(execute))
        acquired, release = asyncio.Event(), asyncio.Event()
        transaction = journal.store.transaction
        @asynccontextmanager
        async def gated():
            async with transaction() as tx:
                if asyncio.current_task().get_name() == "winner" and not acquired.is_set():
                    acquired.set()
                    await release.wait()
                yield tx
        monkeypatch.setattr(journal.store, "transaction", gated)
        winner = asyncio.create_task(apply() if first == "apply" else complete() if first == "complete" else cancellation(),
                                     name="winner")
        await acquired.wait()  # the first contender actually holds the writer
        loser = asyncio.create_task(complete() if first == "apply" else apply())
        release.set()
        await asyncio.gather(winner, loser)
        await journal.steer(STEER, steer(execute))
        await drain(outbox)
        outcome = updates(vcp, "steering.applied") + updates(vcp, "steering.failed")
        assert len(outcome) == 1
        assert outcome[0]["kind"] == ("steering.applied" if first == "apply" else "steering.failed")
        terminal = updates(vcp, "attempt.completed") + updates(vcp, "attempt.cancelled")
        assert len(terminal) == 1
        assert terminal[0]["kind"] == ("attempt.cancelled" if first == "cancel" else "attempt.completed")
        if first != "cancel":
            assert terminal[0]["payload"]["result"]["summary"] == ("Done\nUse the second option" if first == "apply" else "Done")
        await worker.close()
        await journal.store.close()
    asyncio.run(scenario())


def test_codex_is_rejected_even_when_it_advertises_steering(tmp_path):
    async def scenario():
        journal, _, _ = await setup(tmp_path)
        journal.executors["codex"] = journal.executors["fake"]  # deliberately misleading capability advertisement
        execute = command(delay_seconds=30)
        execute["payload"]["agent"]["runner"] = "codex"
        assert (await journal.execute(EXECUTE, execute))["status"] == "accepted"
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await running(journal)
        assert (await journal.steer(STEER, steer(execute)))["error"]["code"] == "UNSUPPORTED_CAPABILITY"
        assert "steering" not in (await state(journal, "attempt_1"))["executor_handle"]
        await worker.close()
        await journal.store.close()
    asyncio.run(scenario())


def test_maximum_message_does_not_make_the_fake_terminal_result_unsendable(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        execute = command(delay_seconds=30)
        await journal.execute(EXECUTE, execute)
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await running(journal)
        assert (await journal.steer(STEER, steer(execute, message="x" * 65536)))["status"] == "accepted"
        async with journal.store.transaction() as tx:
            await journal.finish(tx, await tx.attempt("attempt_1"), "completed", {"result": {"summary": "Done"}}, "exited")
        await drain(outbox)
        summary = updates(vcp, "attempt.completed")[0]["payload"]["result"]["summary"]
        assert len(summary) == 65536 and "[steering sha256:" in summary
        await worker.close()
        await journal.store.close()
    asyncio.run(scenario())
