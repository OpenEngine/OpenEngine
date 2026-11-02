"""Test-only supervision and executor wiring for the ACP fixture agent. Never installed.

`GroupFence` stands in for the cgroup supervisor on any POSIX host: it starts
the fixture in its own process group and fences by killing that group until no
member remains. It is weaker than cgroup v2 (a process may leave its group), so
it is evidence for fixture behavior only, not for Linux process-tree fencing.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

from workspace_daemon.codex import CodexExecutor

AGENT = Path(__file__).with_name("acp_agent.py")


class GroupFence:
    def __init__(self) -> None:
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.fenced: list[dict] = []
        self.confirm = True
        self._next: str | None = None

    def plan(self) -> dict:
        return {"kind": "fixture", "name": uuid4().hex}

    def command(self, handle: dict, argv: list[str]) -> list[str]:
        self._next = handle["name"]
        return argv

    async def spawn(self, *argv, **options):
        process = await asyncio.create_subprocess_exec(*argv, start_new_session=True, **options)
        self.processes[self._next] = process
        return process

    async def fence(self, handle) -> bool:
        self.fenced.append(handle)
        if not self.confirm:
            return False
        process = self.processes.get(handle["name"])
        if process is None:
            return True
        for _ in range(100):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            if process.returncode is None:
                await process.wait()
            await asyncio.sleep(0.02)
        return False


def agent(tmp_path: Path, mode: str = "normal") -> Path:
    """An absolute executable that runs the fixture agent, as an operator would install an adapter."""
    script = tmp_path / f"agent-{mode}"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} -I {AGENT} {tmp_path / 'acp.log'} {mode}\n")
    script.chmod(0o755)
    return script


def codex(tmp_path: Path, mode: str = "normal", fence: GroupFence | None = None, **options) -> CodexExecutor:
    fence = fence or GroupFence()
    executor = CodexExecutor(agent(tmp_path, mode), fence, **options)
    executor.spawn = fence.spawn
    return executor
