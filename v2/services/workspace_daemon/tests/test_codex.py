"""The Codex ACP executor against a deterministic ACP subprocess fixture; no model, credentials or cgroups.

These are fixture results only: `GroupFence` kills a POSIX process group, not a
cgroup-v2 tree, and the fixture is not an authenticated model. The Linux
process-tree and live-model checks are opt-in (`test_linux_supervision.py`).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import tomllib
from pathlib import Path

import pytest

from workspace_daemon import store as journal_store
from workspace_daemon.__main__ import executors, main, parser
from workspace_daemon.codex import CHUNK, CodexExecutor
from workspace_daemon.context import read_instruction
from workspace_daemon.executor import ExecutionWorker, FakeExecutor
from workspace_daemon.initialization import locations
from workspace_daemon.journal import Journal
from workspace_daemon.outbox import UpdateOutbox
from workspace_daemon.supervision import SupervisionUnavailable
from workspace_daemon.tests.acp_fixture import GroupFence, codex
from workspace_daemon.tests.test_journal import BINDING_TARGET, EXECUTE, Vcp, command, drain

DAEMON = Path(__file__).parents[1]


def task(attempt="attempt_1", command_id="rtc_1", mode="new", model=None, instruction="Summarize the repository"):
    value = command(attempt, command_id, mode=mode)
    value["payload"]["agent"] = {"runner": "codex", **({"model": model} if model else {})}
    value["payload"]["task"] = {"instruction": instruction, "inputs": {"ticket": "OE-1"}}
    value["payload"]["context"]["working_directory"] = "src"
    return value


async def setup(tmp_path, executor, **options):
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text("Report what you find. Do not edit files.")
    directory, root = locations(tmp_path / "journal", root)
    database = journal_store.database(directory)
    journal_store.upgrade(database)
    configured = {"codex": executor} if isinstance(executor, CodexExecutor) else {"fake": executor}
    journal = Journal(journal_store.JournalStore(database), BINDING_TARGET, root, configured, **options)
    await journal.recover()
    vcp = Vcp()
    outbox = UpdateOutbox(journal, vcp)
    await drain(outbox)
    return journal, vcp, outbox


async def run(journal, value, worker=None):
    reply = await journal.execute(EXECUTE, value)
    assert reply["status"] == "accepted", reply
    worker = worker or ExecutionWorker(journal)
    assert await worker.run_once() == 1
    await asyncio.wait_for(asyncio.gather(*worker.tasks.values()), 30)
    return worker


async def state(journal, attempt_id="attempt_1"):
    async with journal.store.read() as tx:
        return await tx.attempt(attempt_id)


def frames(tmp_path):
    return [json.loads(line) for line in (tmp_path / "acp.log").read_text().splitlines()]


def updates(vcp, kind=None):
    values = [json.loads(wire) for wire in vcp.committed]
    return [u for u in values if kind is None or u["kind"] == kind]


def test_context_progress_usage_permission_denial_and_continuation(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path))
        await run(journal, task(model="fixture-large"))
        await drain(outbox)
        assert [u["kind"] for u in updates(vcp)] == [
            "runtime.ready", "attempt.started", "attempt.progress", "attempt.progress", "attempt.completed"]
        progress = [u["payload"] for u in updates(vcp, "attempt.progress")]
        # Streamed fragments are coalesced into one message for the turn.
        assert progress[0]["text"] == "Inspected workspace. Found the requested result."
        assert progress[1]["usage"] == {"input_tokens": 10, "output_tokens": 7, "cache_read_tokens": 3}  # no cost
        assert progress[1]["period_start"] <= progress[1]["period_end"]
        assert updates(vcp, "attempt.completed")[0]["payload"]["result"]["summary"] == \
            "Inspected workspace. Found the requested result."
        log = frames(tmp_path)
        started = log[0]["started"]
        assert started["cwd"] == str(journal.root / "src") and "NATS_URL" not in started["environment"]
        methods = [f.get("method") for f in log if "method" in f]
        assert methods == ["initialize", "session/new", "session/set_mode", "session/set_config_option", "session/prompt"]
        initialize = next(f for f in log if f.get("method") == "initialize")["params"]["clientCapabilities"]
        assert initialize == {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
        assert next(f for f in log if f.get("method") == "session/set_mode")["params"]["modeId"] == "read-only"
        assert next(f for f in log if f.get("method") == "session/set_config_option")["params"]["value"] == "fixture-large"
        prompt = next(f for f in log if f.get("method") == "session/prompt")["params"]["prompt"][0]["text"]
        assert "Do not edit files" in prompt and "Summarize the repository" in prompt and "OE-1" in prompt
        replies = {f["id"]: f for f in log if "id" in f and "method" not in f}
        assert replies["permission_1"]["result"] == {"outcome": {"outcome": "selected", "optionId": "deny"}}
        assert replies["fs_1"]["error"]["code"] == -32601
        async with journal.store.read() as tx:
            first = await tx.session("session_1")
        assert first["executor_session_id"].startswith("acp_") and first["executor_resumable"]

        # Continue after a daemon restart: the persisted conversation is loaded, not replaced.
        await journal.store.close()
        restarted = Journal(journal_store.JournalStore(journal_store.database(tmp_path / "journal")), BINDING_TARGET,
                            journal.root, {"codex": codex(tmp_path)}, established=True)
        await restarted.recover()
        outbox.journal = restarted
        await drain(outbox)
        await run(restarted, task("attempt_2", "rtc_2", mode="continue", model="fixture-large"))
        await drain(outbox)
        loads = [f for f in frames(tmp_path) if f.get("method") in ("session/new", "session/load")]
        assert [f["method"] for f in loads] == ["session/new", "session/load"]
        assert loads[1]["params"]["sessionId"] == first["executor_session_id"]
        texts = [u["payload"].get("text") for u in updates(vcp, "attempt.progress")]
        assert "Replayed history" not in texts and texts.count("Inspected workspace. Found the requested result.") == 2
        assert (await state(restarted, "attempt_2"))["state"] == "completed"
        await restarted.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["no_resume", "never_opened", "forgotten"])
def test_missing_or_unloadable_conversations_are_never_replaced(tmp_path, case):
    async def scenario():
        mode = "no_resume" if case == "no_resume" else "exit" if case == "never_opened" else "normal"
        fence = GroupFence()
        journal, _, _ = await setup(tmp_path, codex(tmp_path, mode, fence))
        first = task(model="missing-model") if case == "never_opened" else task()
        await run(journal, first)
        if case == "forgotten":
            (tmp_path / "acp.sessions").unlink()  # the adapter lost the conversation
        continued = task("attempt_2", "rtc_2", mode="continue")
        if case != "forgotten":
            reply = await journal.execute(EXECUTE, continued)
            assert reply["error"]["code"] == "INVALID_ARGUMENT"
        else:
            await run(journal, continued)
            attempt = await state(journal, "attempt_2")
            assert attempt["state"] == "failed"
            methods = [f.get("method") for f in frames(tmp_path) if "method" in f]
            assert methods.count("session/new") == 1 and methods.count("session/prompt") == 1
            assert all(p.returncode is not None for p in fence.processes.values())
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode, model", [("normal", "unknown-model"), ("no_read_only", None)])
def test_unsupported_model_or_mode_fails_launch_without_prompting(tmp_path, mode, model):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, mode, fence))
        await run(journal, task(model=model))
        await drain(outbox)
        failed = updates(vcp, "attempt.failed")[0]
        assert (failed["payload"]["reason"], failed["termination"]) == ("launch_failed", "killed")
        assert not updates(vcp, "attempt.started")
        assert "session/prompt" not in [f.get("method") for f in frames(tmp_path)]
        assert all(p.returncode is not None for p in fence.processes.values())
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["stop_reading", "hang", "exit"])
def test_blocked_io_and_crashes_fail_only_after_the_tree_is_fenced(tmp_path, mode):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, mode, fence, prompt_timeout=0.5))
        # Larger than a pipe buffer, so a reader that stopped blocks the write itself.
        await run(journal, task(instruction="x" * 60000))
        await drain(outbox)
        failed = updates(vcp, "attempt.failed")[0]
        assert (failed["payload"]["reason"], failed["termination"]) == ("executor_failed", "killed")
        assert len(fence.fenced) == 1 and all(p.returncode is not None for p in fence.processes.values())
        await journal.store.close()
    asyncio.run(scenario())


def test_unexpected_wait_errors_fail_the_fenced_attempt_rather_than_lose_it(tmp_path, monkeypatch):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, fence=fence))

        async def broken(attempt_id, value):
            raise ValueError(value)  # for example a journal or encoding error
        monkeypatch.setattr(journal, "progress", broken)
        await run(journal, task())
        await drain(outbox)
        failed = updates(vcp, "attempt.failed")[0]
        assert (failed["payload"]["reason"], failed["termination"]) == ("executor_failed", "killed")
        assert len(fence.fenced) == 1 and all(p.returncode is not None for p in fence.processes.values())
        await journal.store.close()
    asyncio.run(scenario())


def test_unconfirmed_fencing_keeps_ownership_until_termination_is_confirmed(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, "exit", fence))
        fence.confirm = False
        worker = await run(journal, task())
        assert (await state(journal))["state"] == "running"
        # Still owned: the session stays busy and capacity stays used.
        busy = await journal.execute(EXECUTE, task("attempt_2", "rtc_2", mode="continue"))
        assert busy["error"]["code"] == "SESSION_BUSY"
        worker.backoff._pending.clear()
        assert await worker.run_once() == 0
        await asyncio.gather(*worker.tasks.values())
        assert (await state(journal))["state"] == "running" and len(fence.fenced) == 2
        fence.confirm = True
        worker.backoff._pending.clear()
        await worker.run_once()
        await asyncio.gather(*worker.tasks.values())
        attempt = await state(journal)
        assert (attempt["state"], attempt["launch_count"]) == ("failed", 1)
        await drain(outbox)
        failed = updates(vcp, "attempt.failed")[0]
        assert (failed["payload"]["reason"], failed["termination"]) == ("executor_lost", "killed")
        assert [f.get("method") for f in frames(tmp_path)].count("session/prompt") == 1  # never re-executed
        await journal.store.close()
    asyncio.run(scenario())


def test_failed_terminal_journaling_is_resolved_by_fencing_not_reexecution(tmp_path, monkeypatch):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, fence=fence))
        finish = journal.finish

        async def lost(tx, attempt, state, payload, termination):
            if state == "completed":
                raise OSError(28)  # ENOSPC
            await finish(tx, attempt, state, payload, termination)
        monkeypatch.setattr(journal, "finish", lost)
        worker = await run(journal, task())
        assert (await state(journal))["state"] == "running"
        await worker.run_once()
        await asyncio.gather(*worker.tasks.values())
        await drain(outbox)
        assert [u["kind"] for u in updates(vcp)][-1] == "attempt.failed"
        assert updates(vcp, "attempt.failed")[0]["payload"]["reason"] == "executor_lost"
        assert [f.get("method") for f in frames(tmp_path)].count("session/prompt") == 1
        await journal.store.close()
    asyncio.run(scenario())


def test_restart_mid_prompt_fences_the_journaled_launch_and_never_reprompts(tmp_path):
    async def scenario():
        fence = GroupFence()
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, "hang", fence))
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal)
        await worker.run_once()
        for _ in range(500):
            if (await state(journal))["state"] == "running":
                break
            await asyncio.sleep(0.01)
        attempt = await state(journal)
        assert attempt["state"] == "running" and attempt["executor_handle"]["name"] in fence.processes
        # The daemon "crashes": a new process recovers from the journal alone.
        restarted = Journal(journal.store, BINDING_TARGET, journal.root, {"codex": codex(tmp_path, "hang", fence)},
                            established=True)
        evidence = await restarted.recover()
        assert [e["decision"] for e in evidence] == ["fenced_failed"]
        assert fence.fenced[-1] == attempt["executor_handle"]
        assert all(p.returncode is not None for p in fence.processes.values())
        for task_ in worker.tasks.values():
            task_.cancel()
        await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
        assert (await state(restarted))["state"] == "failed"
        assert [f.get("method") for f in frames(tmp_path)].count("session/prompt") == 1
        await journal.store.close()
    asyncio.run(scenario())


def test_progress_waits_for_outbox_capacity_and_drops_nothing(tmp_path):
    async def scenario():
        journal, vcp, outbox = await setup(tmp_path, codex(tmp_path, "flood"), max_pending_updates=4)
        assert (await journal.execute(EXECUTE, task()))["status"] == "accepted"
        worker = ExecutionWorker(journal)
        await worker.run_once()
        for _ in range(300):
            async with journal.store.read() as tx:
                pending = await tx.pending_updates()
            assert pending <= 4
            await asyncio.sleep(0.01)
        assert worker.tasks, "the producer must wait while the outbox is full"
        while worker.tasks:
            await drain(outbox)
            await asyncio.sleep(0.01)
        await drain(outbox)
        texts = [u["payload"]["text"] for u in updates(vcp, "attempt.progress") if "text" in u["payload"]]
        assert texts == [f"{i:02d}" * (CHUNK // 2) for i in range(40)] + ["Inspected workspace. Found the requested result."]
        assert updates(vcp)[-1]["kind"] == "attempt.completed"
        await journal.store.close()
    asyncio.run(scenario())


def test_fencing_removes_the_whole_fixture_process_group(tmp_path):
    async def scenario():
        journal, _, _ = await setup(tmp_path, codex(tmp_path, "child"))
        await run(journal, task())
        child = int((tmp_path / "acp.child").read_text())
        assert (await state(journal))["state"] == "completed"
        for _ in range(100):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("the agent's child outlived fencing")
        await journal.store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["launch", "wait"])
def test_launch_and_wait_exceptions_are_not_evidence_of_termination(tmp_path, stage):
    class Uncertain(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.fenced = []

        async def launch(self, *args):
            if stage == "launch":
                raise RuntimeError  # spawned, then lost the pipe
            return await super().launch(*args)

        async def wait(self, *args):
            raise RuntimeError  # lost the process

        async def fence(self, attempt):
            self.fenced.append(attempt["state"])
            return True

    async def scenario():
        executor = Uncertain()
        journal, vcp, outbox = await setup(tmp_path, executor)
        value = command()
        await run(journal, value)
        assert (await state(journal))["state"] == ("launching" if stage == "launch" else "running")
        worker = ExecutionWorker(journal)
        await worker.run_once()
        await asyncio.gather(*worker.tasks.values())
        assert executor.fenced == ["launching" if stage == "launch" else "running"]
        await drain(outbox)
        failed = updates(vcp, "attempt.failed")[0]
        assert (failed["payload"]["reason"], failed["termination"]) == ("executor_lost", "killed")
        await journal.store.close()
    asyncio.run(scenario())


def test_instruction_reads_refuse_symlinks_even_inside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret").write_text("outside")
    (root / "AGENTS.md").symlink_to(tmp_path / "secret")
    with pytest.raises(OSError):
        read_instruction(root, root / "AGENTS.md")


def test_installed_runtime_has_no_fence_bypass_or_langgraph(tmp_path):
    options = {option for action in parser()._actions for option in action.option_strings}
    assert not {option for option in options if "fence" in option or "unsafe" in option or "test" in option} - {
        "--local-test-account"}
    project = tomllib.loads((DAEMON / "pyproject.toml").read_text())["project"]
    assert not any("langgraph" in dependency.lower() for dependency in project["dependencies"])
    shipped = tomllib.loads((DAEMON / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "tests" not in shipped["only-include"]
    for path in DAEMON.glob("*.py"):
        imports = {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Import)
                   for alias in node.names} | {node.module or "" for node in ast.walk(ast.parse(path.read_text()))
                                                if isinstance(node, ast.ImportFrom)}
        assert not {name for name in imports if "langgraph" in name or "tests" in name}, path
        assert "GroupFence" not in path.read_text()
    # Real execution fails closed where cgroup-v2 supervision as root is unavailable.
    root = tmp_path / "root"
    root.mkdir()
    args = parser().parse_args(["--journal-directory", str(tmp_path / "journal"), "--workspace-root", str(root),
                                "--workspace-id", "ws_test", "--runtime-id", "rt_test", "--generation", "1",
                                "--codex-acp", "/usr/local/bin/codex-acp", "--local-test-account", "--initialize",
                                "--ready-file", str(tmp_path / "ready")])
    with pytest.raises((SupervisionUnavailable, OSError)):
        executors(args, tmp_path / "journal", root)
    argv = [option for option in [
        "--journal-directory", str(tmp_path / "journal"), "--workspace-root", str(root), "--workspace-id", "ws_test",
        "--runtime-id", "rt_test", "--generation", "1", "--codex-acp", "/usr/local/bin/codex-acp", "--cgroup-root",
        str(tmp_path), "--agent-uid", "65534", "--agent-gid", "65534", "--agent-home", str(tmp_path),
        "--local-test-account", "--initialize", "--ready-file", str(tmp_path / "ready"), "--nats-url", "nats://127.0.0.1:1"]]
    assert main(argv) == 1
    assert not (tmp_path / "ready").exists() and not (tmp_path / "journal" / "binding.json").exists()
