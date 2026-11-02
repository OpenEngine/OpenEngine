"""The executor port, a deterministic fake executor and the worker that launches journaled attempts.

Launch intent (`launching`, `launch_count`) commits before the executor's side
effect; `running` and the `attempt.started` update commit together once launch
returns. An attempt found `launching` or `running` after a restart is never
launched again: the executor must confirm it cannot continue (`fence`), and
the journal then records it failed with `executor_lost`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

SETTINGS = {"outcome", "delay_seconds"}


class ExecutorPort(Protocol):
    progress_kinds: tuple[str, ...]

    def validate(self, payload: dict[str, Any]) -> None:
        """Raise ValueError for settings this executor does not allow. No side effects."""
        ...

    async def launch(self, attempt_id: str, command: dict[str, Any], root: Path) -> dict[str, Any]:
        """Start the work and return a durable handle. Raise only if nothing started."""
        ...

    async def wait(self, attempt_id: str, handle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """`("completed", {"result": ...})` or `("failed", {"error": ..., "reason": "executor_failed"})`."""
        ...

    async def fence(self, attempt: dict[str, Any]) -> bool:
        """After a restart: True only once the interrupted work provably cannot continue."""
        ...


class FakeExecutor:
    """In-process stand-in; it runs no agent, model or ACP subprocess.

    Its work lives in this daemon's event loop, so once a restarted daemon holds
    the journal's exclusive lock the previous process, and its work, has ended.
    `effects`, if given, gets one line per launch: test evidence of no relaunch.
    """

    progress_kinds: tuple[str, ...] = ()

    def __init__(self, effects: Path | None = None) -> None:
        self.effects = effects

    def validate(self, payload: dict[str, Any]) -> None:
        settings = payload["agent"].get("settings", {})
        delay = settings.get("delay_seconds", 0.05)
        if (set(settings) - SETTINGS or settings.get("outcome", "completed") not in ("completed", "failed")
                or type(delay) not in (int, float) or not 0 <= delay <= 60):
            raise ValueError(settings)

    async def launch(self, attempt_id: str, command: dict[str, Any], root: Path) -> dict[str, Any]:
        if self.effects is not None:
            with self.effects.open("a") as file:
                file.write(attempt_id + "\n")
        return {"kind": "in_process", "settings": command["payload"]["agent"].get("settings", {})}

    async def wait(self, attempt_id: str, handle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        settings = handle["settings"]
        await asyncio.sleep(settings.get("delay_seconds", 0.05))
        if settings.get("outcome", "completed") == "completed":
            return "completed", {"result": {"summary": "Fake executor completed the task."}}
        return "failed", {"error": {"code": "PROVIDER_ERROR", "message": "Simulated executor failure", "retryable": False},
                          "reason": "executor_failed"}

    async def fence(self, attempt: dict[str, Any]) -> bool:
        return True


class ExecutionWorker:
    """Launches each recorded attempt once, independently of command reception and inspect."""

    def __init__(self, journal, poll_seconds: float = 0.05) -> None:
        self.journal, self.poll_seconds = journal, poll_seconds
        self.tasks: dict[str, asyncio.Task] = {}
        self._worker: asyncio.Task | None = None

    async def run_once(self) -> int:
        async with self.journal.store.read() as tx:
            recorded = [a["command"]["target"]["attempt_id"] for a in await tx.attempts("recorded")]
        launched = 0
        for attempt_id in recorded:
            if attempt_id not in self.tasks:
                self.tasks[attempt_id] = asyncio.create_task(self._run(attempt_id))
                self.tasks[attempt_id].add_done_callback(lambda _, key=attempt_id: self.tasks.pop(key, None))
                launched += 1
        return launched

    async def _run(self, attempt_id: str) -> None:
        journal = self.journal
        async with journal.store.transaction() as tx:
            attempt = await tx.attempt(attempt_id)
            if attempt is None or attempt["state"] != "recorded":
                return
            attempt.update(state="launching", launch_count=attempt["launch_count"] + 1)
            await tx.save_attempt(attempt)
        command = attempt["command"]
        executor = journal.executors[command["payload"]["agent"]["runner"]]
        try:
            handle = await executor.launch(attempt_id, command, journal.root)
        except Exception:
            log.exception("executor launch failed", extra={"daemon.attempt_id": attempt_id})
            async with journal.store.transaction() as tx:
                await journal.finish(tx, await tx.attempt(attempt_id), "failed", {"error": {
                    "code": "PROVIDER_ERROR", "message": "Executor failed to launch", "retryable": False},
                    "reason": "launch_failed"}, "never_started")
            return
        async with journal.store.transaction() as tx:
            attempt = await tx.attempt(attempt_id)
            attempt.update(state="running", executor_handle=handle)
            await journal.append(tx, "attempt.started", {}, attempt)
            await tx.save_attempt(attempt)
        try:
            state, payload = await executor.wait(attempt_id, handle)
        except Exception:
            log.exception("executor failed", extra={"daemon.attempt_id": attempt_id})
            state, payload = "failed", {"error": {"code": "PROVIDER_ERROR", "message": "Executor failed", "retryable": False},
                                        "reason": "executor_failed"}
        async with journal.store.transaction() as tx:
            await journal.finish(tx, await tx.attempt(attempt_id), state, payload, "exited")

    def start(self) -> None:
        self._worker = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("execution worker failed; retrying")
            await asyncio.sleep(self.poll_seconds)

    async def close(self) -> None:
        # Interrupted attempts stay launching/running; the next start fences them.
        tasks = [task for task in (self._worker, *self.tasks.values()) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
