"""Opt-in Linux checks of the installed supervisor; skipped unless an operator provisions the host.

Run as root on Linux with a delegated cgroup-v2 directory and an unprivileged
agent account:

    OE_DAEMON_TEST_CGROUP_ROOT=/sys/fs/cgroup/oe-test OE_DAEMON_TEST_AGENT_UID=… \\
    OE_DAEMON_TEST_AGENT_GID=… OE_DAEMON_TEST_AGENT_HOME=/home/oe-agent \\
    uv run --project v2 pytest v2/services/workspace_daemon/tests/test_linux_supervision.py

The live-model case additionally needs `OE_DAEMON_TEST_CODEX_ACP`, the absolute
path of a Codex ACP adapter that is already authenticated in the agent's HOME
and a workspace readable by that account (`OE_DAEMON_TEST_WORKSPACE`). It is
the only test that exercises an authenticated model; fixture tests do not.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from workspace_daemon.codex import CodexExecutor
from workspace_daemon.supervision import CgroupSupervisor
from workspace_daemon.tests.test_codex import run, setup, task, updates
from workspace_daemon.tests.test_journal import drain

REQUIRED = ("OE_DAEMON_TEST_CGROUP_ROOT", "OE_DAEMON_TEST_AGENT_UID", "OE_DAEMON_TEST_AGENT_GID", "OE_DAEMON_TEST_AGENT_HOME")
pytestmark = pytest.mark.skipif(
    os.uname().sysname != "Linux" or not all(os.environ.get(key) for key in REQUIRED),
    reason="needs a provisioned Linux cgroup-v2 host, root and an agent account (" + ", ".join(REQUIRED) + ")")


def supervisor(*protected: Path) -> CgroupSupervisor:
    return CgroupSupervisor(Path(os.environ["OE_DAEMON_TEST_CGROUP_ROOT"]), uid=int(os.environ["OE_DAEMON_TEST_AGENT_UID"]),
                            gid=int(os.environ["OE_DAEMON_TEST_AGENT_GID"]), home=Path(os.environ["OE_DAEMON_TEST_AGENT_HOME"]),
                            protected=protected)


def test_fencing_empties_and_removes_the_whole_process_tree():
    async def scenario():
        fence = supervisor()
        handle = fence.plan()
        group = fence.root / handle["name"]
        # A tree that outlives its parent: the shell exits, its grandchildren keep running.
        process = await asyncio.create_subprocess_exec(*fence.command(handle, ["/bin/sh", "-c", "(sleep 300 &) ; sleep 300"]))
        try:
            for _ in range(200):
                if "populated 1" in (group / "cgroup.events").read_text():
                    break
                await asyncio.sleep(0.01)
            procs = (group / "cgroup.procs").read_text().split()
            assert len(procs) >= 2
            assert all(os.stat(f"/proc/{pid}").st_uid == int(os.environ["OE_DAEMON_TEST_AGENT_UID"]) for pid in procs)
            assert await fence.fence(handle)
            assert not group.exists()
            await asyncio.wait_for(process.wait(), 5)
            for pid in procs:
                assert not Path(f"/proc/{pid}").exists() or "Z" in Path(f"/proc/{pid}/stat").read_text().split()[2]
            # A launch identity that was fenced first can never start: its cgroup is gone.
            assert await fence.fence(handle)
        finally:
            await fence.fence(handle)
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get("OE_DAEMON_TEST_CODEX_ACP") or not os.environ.get("OE_DAEMON_TEST_WORKSPACE"),
                    reason="needs an installed, authenticated Codex ACP adapter and agent-readable workspace "
                           "(OE_DAEMON_TEST_CODEX_ACP, OE_DAEMON_TEST_WORKSPACE)")
def test_live_codex_task_runs_read_only_and_reports_progress(tmp_path):
    async def scenario():
        workspace = Path(os.environ["OE_DAEMON_TEST_WORKSPACE"])
        executor = CodexExecutor(Path(os.environ["OE_DAEMON_TEST_CODEX_ACP"]), supervisor(workspace), prompt_timeout=300)
        journal, vcp, outbox = await setup(tmp_path, executor)
        journal.root = workspace.resolve()
        value = task(instruction="Reply with exactly OE_OK. Do not run commands or change files.")
        value["payload"]["context"] = {"working_directory": ".", "instruction_files": [], "references": []}
        await run(journal, value)
        await drain(outbox)
        completed = updates(vcp, "attempt.completed")
        assert completed and "OE_OK" in completed[0]["payload"]["result"]["summary"]
        assert updates(vcp, "attempt.progress")
        await journal.store.close()
    asyncio.run(scenario())
