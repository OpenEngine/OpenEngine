"""Daemon journal, initialization and recovery against a temporary workspace root; no broker or VM."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
import sqlalchemy as sa

from openengine.contracts.runtime import validate_wire_message
from openengine.persistence.migrations import downgrade

from workspace_daemon import journal as journal_module
from workspace_daemon import store as journal_store
from workspace_daemon.__main__ import main
from workspace_daemon.executor import ExecutionWorker, FakeExecutor
from workspace_daemon.initialization import BINDING, PENDING, Initialization, RecoveryRefused, locations
from workspace_daemon.journal import Journal, encode
from workspace_daemon.outbox import UpdateOutbox

BINDING_TARGET = {"workspace_id": "ws_test", "runtime_id": "rt_test", "runtime_generation": 1}
EXECUTE = "oe.internal.v1.runtime.runtimes.rt_test.execute"


def command(attempt="attempt_1", command_id="rtc_1", session="session_1", mode="new", **settings):
    return {"command_id": command_id, "kind": "execute", "target": {**BINDING_TARGET, "session_id": session, "attempt_id": attempt},
            "execution": {"command_id": command_id, "cause": {"operation": {"operation_id": "op_1", "command_id": "cmd_1"},
                                                               "causal_path": ["cmd_1"]}},
            "payload": {"agent": {"runner": "fake", "settings": {"delay_seconds": 0, **settings}},
                        "task": {"instruction": "Test"}, "session_mode": mode,
                        "context": {"working_directory": ".", "instruction_files": ["AGENTS.md"], "references": []}}}


class Vcp:
    """Commits every update in order, like VCP, optionally losing replies or reporting gaps."""

    def __init__(self):
        self.committed, self.lose, self.gap = [], 0, None

    async def send(self, subject, wire):
        message = json.loads(wire)
        ack = {"event_id": message["event_id"], "target": {k: message["target"][k] for k in BINDING_TARGET},
               "sequence": message["sequence"], "status": "committed"}
        if self.gap is not None and message["sequence"] > self.gap:
            ack.update(status="rejected", expected_sequence=self.gap,
                       error={"code": "EVENT_GAP", "message": "replay", "retryable": True})
            self.gap = None
        elif message["sequence"] == len(self.committed) + 1:
            self.committed.append(wire)
        else:
            assert wire in self.committed, "out of order"
        if self.lose:
            self.lose -= 1
            from openengine.messaging import Unavailable
            raise Unavailable(subject, "reply lost")
        return encode(ack)


async def drain(outbox):
    for _ in range(50):
        outbox.backoff._pending.clear()
        if not await outbox.run_once():
            async with outbox.journal.store.read() as tx:
                if await tx.oldest_pending() is None:
                    return


async def setup(tmp_path, effects=True, established=False):
    root, directory = tmp_path / "root", tmp_path / "journal"
    root.mkdir(exist_ok=True)
    (root / "AGENTS.md").write_text("instructions")
    directory, root = locations(directory, root)
    database = journal_store.database(directory)
    journal_store.upgrade(database)
    store = journal_store.JournalStore(database)
    journal = Journal(store, BINDING_TARGET, root, {"fake": FakeExecutor(tmp_path / "effects" if effects else None)},
                      established=established)
    await journal.recover()
    vcp = Vcp()
    outbox = UpdateOutbox(journal, vcp)
    await drain(outbox)
    return journal, vcp, outbox


def launches(tmp_path):
    path = tmp_path / "effects"
    return path.read_text().splitlines() if path.exists() else []


def test_execute_is_journaled_once_and_launched_once(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        before_ready = await journal.execute(EXECUTE, command())
        assert before_ready["status"] == "accepted"  # VCP committed runtime.ready in setup
        assert await journal.execute(EXECUTE, command()) == before_ready
        changed = deepcopy(command())
        changed["payload"]["task"]["instruction"] = "Changed"
        assert (await journal.execute(EXECUTE, changed))["error"]["code"] == "COMMAND_ID_CONFLICT"
        assert (await journal.execute(EXECUTE, command(command_id="rtc_2")))["error"]["code"] == "ATTEMPT_CONFLICT"
        worker = ExecutionWorker(journal)
        assert await worker.run_once() == 1
        assert await worker.run_once() == 0
        await asyncio.gather(*worker.tasks.values())
        vcp.lose = 1
        await drain(outbox)
        kinds = [json.loads(wire)["kind"] for wire in vcp.committed]
        assert kinds == ["runtime.ready", "attempt.started", "attempt.completed"]
        assert [json.loads(w)["attempt_sequence"] for w in vcp.committed[1:]] == [1, 2]
        for wire in vcp.committed:
            assert validate_wire_message("update", wire, journal.updates) == []
        assert launches(tmp_path) == ["attempt_1"]
        inspect = await journal.inspect("oe.internal.v1.runtime.runtimes.rt_test.inspect",
                                        {"request_id": "req_1", "target": BINDING_TARGET, "attempt_ids": ["attempt_1", "attempt_x"]})
        assert [(a["known"], a.get("state")) for a in inspect["result"]["attempts"]] == [(True, "completed"), (False, None)]
        continued = await journal.execute(EXECUTE, command("attempt_2", "rtc_3", mode="continue"))
        assert continued["status"] == "accepted"
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("validated", [True, False])
def test_stale_and_wrong_bindings_are_rejected_before_the_journal(tmp_path, monkeypatch, validated):
    """`validated=False` is the negative control: without the binding check a stale command is journaled."""
    async def scenario():
        journal, _, _ = await setup(tmp_path)
        if not validated:
            monkeypatch.setattr(journal_module, "mismatch", lambda *args: None)
        stale, wrong = command(), command(attempt="attempt_2", command_id="rtc_2")
        stale["target"]["runtime_generation"] = 2
        wrong["target"]["workspace_id"] = "ws_other"
        replies = [await journal.execute(EXECUTE, stale), await journal.execute(EXECUTE, wrong)]
        async with journal.store.read() as tx:
            journaled = await tx.all("attempts")
        if validated:
            assert [r["error"]["code"] for r in replies] == ["STALE_RUNTIME", "IDENTITY_MISMATCH"]
            assert journaled == []
        else:
            assert journaled != []
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["escape", "journal", "missing"])
def test_task_context_cannot_reach_the_journal(tmp_path, path):
    async def scenario():
        journal, _, _ = await setup(tmp_path)
        (journal.root / "escape").symlink_to(tmp_path)
        (journal.root / "journal").symlink_to(tmp_path / "journal")
        bad = command()
        bad["payload"]["context"]["working_directory"] = path
        assert (await journal.execute(EXECUTE, bad))["error"]["code"] == "INVALID_ARGUMENT"
        await journal.store.close()
    asyncio.run(scenario())
    (tmp_path / "nested").mkdir()
    for journal_directory, root in [(tmp_path / "root" / "state", tmp_path / "root"), (tmp_path / "nested", tmp_path)]:
        with pytest.raises(RecoveryRefused):
            locations(journal_directory, root)


@pytest.mark.parametrize("stage", ["recorded", "launching", "running"])
def test_restart_launches_recorded_work_once_and_never_relaunches_uncertain_work(tmp_path, stage):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        await journal.execute(EXECUTE, command())
        async with journal.store.transaction() as tx:
            attempt = await tx.get("attempts", "attempt_1")
            if stage != "recorded":
                attempt.update(state=stage, launch_count=1)
                if stage == "running":
                    await journal.append(tx, "attempt.started", {}, attempt)
                await tx.put("attempts", "attempt_1", attempt)
        await journal.store.close()
        restarted = Journal(journal_store.JournalStore(journal_store.database(tmp_path / "journal")), BINDING_TARGET,
                            journal.root, {"fake": FakeExecutor(tmp_path / "effects")}, established=True)
        evidence = await restarted.recover()
        assert evidence[0]["command_id"] == "rtc_1" and evidence[0]["runtime_generation"] == 1
        assert evidence[0]["decision"] == {"recorded": "launch_once"}.get(stage, "fenced_failed")
        worker = ExecutionWorker(restarted)
        assert await worker.run_once() == (stage == "recorded")
        await asyncio.gather(*worker.tasks.values())
        outbox.journal = restarted
        await drain(outbox)
        async with restarted.store.read() as tx:
            attempt = await tx.get("attempts", "attempt_1")
        assert attempt["state"] == ("completed" if stage == "recorded" else "failed")
        assert attempt["launch_count"] == 1
        assert launches(tmp_path) == (["attempt_1"] if stage == "recorded" else [])
        updates = [json.loads(wire) for wire in vcp.committed]
        readies = [u for u in updates if u["kind"] == "runtime.ready"]
        assert len({u["payload"]["journal_id"] for u in readies}) == 1 and len({u["boot_id"] for u in readies}) == 2
        final = [u for u in updates if u["kind"] != "runtime.ready"][-1]
        assert final["kind"] == ("attempt.completed" if stage == "recorded" else "attempt.failed")
        assert stage == "recorded" or (final["payload"]["reason"], final["termination"]) == ("executor_lost", "killed")
        await restarted.store.close()
    asyncio.run(scenario())


def test_unfenceable_interrupted_work_refuses_recovery(tmp_path):
    class Unfenceable(FakeExecutor):
        async def fence(self, attempt):
            return False

    async def scenario():
        journal, _, _ = await setup(tmp_path)
        await journal.execute(EXECUTE, command())
        async with journal.store.transaction() as tx:
            attempt = await tx.get("attempts", "attempt_1")
            attempt.update(state="running", launch_count=1)
            await tx.put("attempts", "attempt_1", attempt)
        restarted = Journal(journal.store, BINDING_TARGET, journal.root, {"fake": Unfenceable()}, established=True)
        with pytest.raises(RecoveryRefused) as refused:
            await restarted.recover()
        assert refused.value.evidence["decision"] == "unresolved"
        assert refused.value.evidence["command_id"] == "rtc_1"
        async with journal.store.read() as tx:
            assert (await tx.get("attempts", "attempt_1"))["state"] == "running"
            assert not (await tx.get("meta", "runtime"))["ready"]
        await journal.store.close()
    asyncio.run(scenario())


def test_gaps_replay_retained_updates_and_rejections_block(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path)
        await journal.execute(EXECUTE, command())
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await asyncio.gather(*worker.tasks.values())
        # VCP lost its last commits (for example a restored database): replay from 2.
        vcp.committed = vcp.committed[:1]
        vcp.gap = 2
        async with journal.store.transaction() as tx:
            await tx.acknowledge(2)
        await drain(outbox)
        assert [json.loads(w)["sequence"] for w in vcp.committed] == [1, 2, 3]

        async def reject(subject, wire):
            message = json.loads(wire)
            return encode({"event_id": message["event_id"], "target": BINDING_TARGET, "sequence": message["sequence"],
                           "status": "rejected", "error": {"code": "STALE_RUNTIME", "message": "retired", "retryable": False}})
        await journal.execute(EXECUTE, command("attempt_2", "rtc_2", mode="continue"))
        await worker.run_once()
        await asyncio.gather(*worker.tasks.values())
        outbox.transport = type("Rejecting", (), {"send": staticmethod(reject)})()
        assert not await outbox.run_once()
        async with journal.store.read() as tx:
            meta = await tx.get("meta", "runtime")
        assert meta["blocked"]["code"] == "STALE_RUNTIME" and not meta["ready"]
        assert (await journal.execute(EXECUTE, command("attempt_3", "rtc_3", mode="continue")))["error"]["code"] == "UNAVAILABLE"
        await journal.store.close()
    asyncio.run(scenario())


def test_initialization_is_resumable_and_an_established_journal_is_never_recreated(tmp_path):
    root, directory = tmp_path / "root", tmp_path / "journal"
    root.mkdir()
    directory, root = locations(directory, root)
    journal_file = journal_store.database(directory).path
    with pytest.raises(RecoveryRefused):
        Initialization(directory, BINDING_TARGET, journal_file, initialize=False)
    Initialization(directory, BINDING_TARGET, journal_file, initialize=True)
    assert (directory / PENDING).exists() and not (directory / BINDING).exists()
    # Interrupted after intent: the same binding resumes; another is refused.
    with pytest.raises(RecoveryRefused):
        Initialization(directory, {**BINDING_TARGET, "runtime_generation": 2}, journal_file, initialize=True)
    resumed = Initialization(directory, BINDING_TARGET, journal_file, initialize=False)
    journal_store.upgrade(journal_store.database(directory))
    resumed.finish()
    assert json.loads((directory / BINDING).read_text()) == BINDING_TARGET and not (directory / PENDING).exists()
    assert (directory / BINDING).stat().st_mode & 0o077 == 0
    # Established without a runtime record (interrupted before recovery committed) fails closed.
    async def scenario():
        store = journal_store.JournalStore(journal_store.database(directory))
        with pytest.raises(RecoveryRefused):
            await Journal(store, BINDING_TARGET, root, {}, established=True).recover()
        await store.close()
    asyncio.run(scenario())
    journal_file.unlink()
    with pytest.raises(RecoveryRefused):
        Initialization(directory, BINDING_TARGET, journal_file, initialize=True)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "empty", "rebound"])
def test_the_process_refuses_damaged_journals_before_listening(tmp_path, damage):
    async def scenario():
        journal, _, _ = await setup(tmp_path)
        await journal.store.close()
    asyncio.run(scenario())
    directory = tmp_path / "journal"
    (directory / BINDING).write_text(json.dumps(BINDING_TARGET))
    path = journal_store.database(directory).path
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"not a journal" * 100)
    elif damage == "empty":
        downgrade(journal_store.MIGRATIONS, journal_store.database(directory), "base")
        engine = sa.create_engine(f"sqlite:///{path}")
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE alembic_version")
        engine.dispose()
    args = ["--journal-directory", str(directory), "--workspace-root", str(tmp_path / "root"), "--workspace-id", "ws_test",
            "--runtime-id", "rt_test", "--generation", "2" if damage == "rebound" else "1", "--local-test-account",
            "--initialize", "--ready-file", str(tmp_path / "ready"), "--nats-url", "nats://127.0.0.1:1"]
    assert main(args) == 2
    assert not (tmp_path / "ready").exists()
