"""The executor port, a deterministic fake executor and the worker that launches journaled attempts.

Launch intent (`launching`, `launch_count`) and the executor's planned launch
identity (`executor_handle`) commit before the executor's side effect;
`running` and the `attempt.started` update commit together once launch returns.
An attempt found `launching` or `running` after a restart is never launched
again: the executor must confirm it cannot continue (`fence`), and the journal
then records it failed with `executor_lost`.

An exception from `launch` or `wait` is not evidence that nothing started or
that the work stopped. Only `LaunchFailed` (raised after the executor itself
confirmed termination) and a returned outcome end an attempt directly. Any
other failure, including a terminal record that could not be journaled, leaves
the attempt `launching` or `running`; the worker then fences it, with backoff,
and records `executor_lost` only once termination is confirmed. Until then the
daemon keeps the attempt, its session and its capacity.

A `cancelling` attempt is stopped in two steps. The executor is first asked to
stop gracefully (`cancel`), and the work may then end on its own; that request,
including any write it makes, shares one deadline (`cancel_seconds`). A fence
the executor is running (`fencing`) is never cut short, and the deadline then
restarts for what follows it (final progress and terminal journaling), so a full
outbox cannot hold the attempt either. At the deadline the worker cancels the
attempt's task, whose executor fences its own process tree; work cancelled
after its fence is fenced again, idempotently, before it is recorded.
Neither step is evidence of termination: the attempt stays `cancelling` until a
terminal record is journaled after a confirmed fence (retried with backoff
like any uncertain attempt), and only then frees its session and capacity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from openengine.messaging.retry import Backoff

log = logging.getLogger(__name__)

SETTINGS = {"outcome", "delay_seconds", "ignore_cancel"}
INTERRUPTED = ("launching", "running", "cancelling")
Progress = Callable[[dict[str, Any]], Awaitable[None]]
Outcome = tuple[str, dict[str, Any], str]


class LaunchFailed(Exception):
    """Launch failed and the executor confirmed nothing it started can still run."""

    def __init__(self, cause: BaseException, termination: str = "never_started") -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.termination = termination


class ExecutorPort(Protocol):
    progress_kinds: tuple[str, ...]
    conversational: bool
    """Continuation needs a persisted, resumable executor conversation."""

    def validate(self, payload: dict[str, Any]) -> None:
        """Raise ValueError for settings this executor does not allow. No side effects."""
        ...

    def plan(self, attempt_id: str) -> dict[str, Any]:
        """A durable launch identity, journaled before `launch` so a restart can fence it. No side effects."""
        ...

    async def launch(self, attempt_id: str, command: dict[str, Any], root: Path, handle: dict[str, Any],
                     session: dict[str, Any] | None) -> dict[str, Any] | None:
        """Start the work under `handle`. Returns the executor conversation to persist
        (`{"executor_session_id", "resumable"}`) or None. Raise `LaunchFailed` only once
        nothing started can still run."""
        ...

    async def wait(self, attempt_id: str, handle: dict[str, Any], progress: Progress) -> Outcome:
        """`(state, payload, termination)` once the work provably stopped."""
        ...

    async def cancel(self, attempt_id: str) -> None:
        """Ask the attempt's work to stop on its own. Best effort: never proof of termination; the caller bounds it."""
        ...

    def fencing(self, attempt_id: str) -> bool:
        """True while `launch` or `wait` fences the attempt's process tree, which must not be interrupted."""
        ...

    async def fence(self, attempt: dict[str, Any]) -> bool:
        """True only once the attempt's work provably cannot continue."""
        ...


class FakeExecutor:
    """In-process stand-in; it runs no agent, model or ACP subprocess.

    Its work lives in this daemon's event loop, so once a restarted daemon holds
    the journal's exclusive lock the previous process, and its work, has ended.
    `effects`, if given, gets one line per launch: test evidence of no relaunch.
    `cancel` ends the wait early unless `ignore_cancel` is set, which stands in
    for work that does not stop on request and must be fenced at the deadline.
    """

    progress_kinds: tuple[str, ...] = ()
    conversational = False
    steering = True

    def __init__(self, effects: Path | None = None) -> None:
        self.effects = effects
        self.stopping: dict[str, asyncio.Event] = {}

    def validate(self, payload: dict[str, Any]) -> None:
        settings = payload["agent"].get("settings", {})
        delay = settings.get("delay_seconds", 0.05)
        if (set(settings) - SETTINGS or settings.get("outcome", "completed") not in ("completed", "failed")
                or type(delay) not in (int, float) or not 0 <= delay <= 60
                or type(settings.get("ignore_cancel", False)) is not bool):
            raise ValueError(settings)

    @staticmethod
    def apply_steering(handle: dict[str, Any], command: dict[str, Any]) -> None:
        """Pure fake state mutation; the journal commits it with the outcome update."""
        handle.setdefault("steering", []).append({"command_id": command["command_id"],
                                                  "message": command["payload"]["message"]})

    def plan(self, attempt_id: str) -> dict[str, Any]:
        return {"kind": "in_process"}

    async def launch(self, attempt_id: str, command: dict[str, Any], root: Path, handle: dict[str, Any],
                     session: dict[str, Any] | None) -> None:
        if self.effects is not None:
            with self.effects.open("a") as file:
                file.write(attempt_id + "\n")
        handle["settings"] = command["payload"]["agent"].get("settings", {})

    async def wait(self, attempt_id: str, handle: dict[str, Any], progress: Progress) -> Outcome:
        settings = handle["settings"]
        stop = asyncio.Event() if settings.get("ignore_cancel") else self.stopping.setdefault(attempt_id, asyncio.Event())
        try:
            async with asyncio.timeout(settings.get("delay_seconds", 0.05)):
                await stop.wait()
        except TimeoutError:
            pass
        finally:
            self.stopping.pop(attempt_id, None)
        if stop.is_set():
            return "failed", {"error": {"code": "PROVIDER_ERROR", "message": "Fake executor stopped on request",
                                        "retryable": False}, "reason": "executor_failed"}, "exited"
        if settings.get("outcome", "completed") == "completed":
            return "completed", {"result": {"summary": "Fake executor completed the task."}}, "exited"
        return "failed", {"error": {"code": "PROVIDER_ERROR", "message": "Simulated executor failure", "retryable": False},
                          "reason": "executor_failed"}, "exited"

    async def cancel(self, attempt_id: str) -> None:
        self.stopping.setdefault(attempt_id, asyncio.Event()).set()

    def fencing(self, attempt_id: str) -> bool:
        return False  # it has no process tree

    async def fence(self, attempt: dict[str, Any]) -> bool:
        self.stopping.pop(attempt["command"]["target"]["attempt_id"], None)
        return True


class ExecutionWorker:
    """Launches each recorded attempt once, independently of command reception and inspect,
    and fences any launching, running or cancelling attempt no task of this process still owns."""

    def __init__(self, journal, poll_seconds: float = 0.05, cancel_seconds: float = 5.0) -> None:
        self.journal, self.poll_seconds, self.cancel_seconds = journal, poll_seconds, cancel_seconds
        self.tasks: dict[str, asyncio.Task] = {}
        self.stopping: dict[str, asyncio.Task] = {}
        self.launches: set[str] = set()
        self.backoff = Backoff(initial=1.0, maximum=30.0)
        self._worker: asyncio.Task | None = None

    async def run_once(self) -> int:
        """Start due work; returns how many recorded attempts were launched."""
        async with self.journal.store.read() as tx:
            recorded = await tx.attempt_ids("recorded")
            orphaned = await tx.attempt_ids(*INTERRUPTED)
            cancelling = await tx.attempt_ids("cancelling")
        for attempt_id in cancelling:
            # Only a live launch is stopped; a fence already in progress is left to finish.
            task = self.tasks.get(attempt_id)
            if task is not None and attempt_id in self.launches and attempt_id not in self.stopping:
                self.stopping[attempt_id] = asyncio.create_task(self._stop(attempt_id, task))
                self.stopping[attempt_id].add_done_callback(lambda _, key=attempt_id: self.stopping.pop(key, None))
        launched = 0
        for attempt_id in recorded:
            if self._start(attempt_id, self._run(attempt_id)):
                self.launches.add(attempt_id)
                self.tasks[attempt_id].add_done_callback(lambda _, key=attempt_id: self.launches.discard(key))
                launched += 1
        for attempt_id in orphaned:
            if attempt_id not in self.tasks and self.backoff.due(attempt_id):
                self._start(attempt_id, self._resolve(attempt_id))
        return launched

    def _start(self, attempt_id: str, work) -> bool:
        if attempt_id in self.tasks:
            work.close()
            return False
        self.tasks[attempt_id] = asyncio.create_task(self._guard(attempt_id, work))
        self.tasks[attempt_id].add_done_callback(lambda _, key=attempt_id: self.tasks.pop(key, None))
        return True

    @staticmethod
    async def _guard(attempt_id: str, work) -> None:
        try:
            await work
        except Exception:
            # Not evidence of termination: the attempt keeps its state and is fenced next.
            log.exception("executor outcome is uncertain", extra={"daemon.attempt_id": attempt_id})

    async def _run(self, attempt_id: str) -> None:
        journal = self.journal
        async with journal.store.transaction() as tx:
            attempt = await tx.attempt(attempt_id)
            if attempt is None or attempt["state"] != "recorded":
                return
            command = attempt["command"]
            executor = journal.executors[command["payload"]["agent"]["runner"]]
            session = await tx.session(command["target"]["session_id"])
            handle = executor.plan(attempt_id)
            attempt.update(state="launching", launch_count=attempt["launch_count"] + 1, executor_handle=handle)
            await tx.save_attempt(attempt)
        try:
            conversation = await executor.launch(attempt_id, command, journal.root, handle, session)
        except LaunchFailed as failed:
            log.warning("executor launch failed: %s", failed, extra={"daemon.attempt_id": attempt_id})
            async with journal.store.transaction() as tx:
                await journal.finish(tx, await tx.attempt(attempt_id), "failed", {"error": {
                    "code": "PROVIDER_ERROR", "message": "Executor failed to launch", "retryable": False},
                    "reason": "launch_failed"}, failed.termination)
            return
        async with journal.store.transaction() as tx:
            attempt = await tx.attempt(attempt_id)
            if attempt["state"] == "launching":
                attempt["state"] = "running"  # a cancel journaled meanwhile keeps it cancelling
            attempt["executor_handle"] = handle
            if conversation is not None:
                await tx.save_executor_session(command["target"]["session_id"], conversation["executor_session_id"],
                                               conversation["resumable"])
            await journal.append(tx, "attempt.started", {}, attempt)
            await tx.save_attempt(attempt)
        state, payload, termination = await executor.wait(
            attempt_id, handle, lambda value: journal.progress(attempt_id, value))
        async with journal.store.transaction() as tx:
            await journal.finish(tx, await tx.attempt(attempt_id), state, payload, termination)

    async def _stop(self, attempt_id: str, task: asyncio.Task) -> None:
        """Graceful cancellation within one deadline, then the task is cancelled so its executor fences."""
        evidence = {"daemon.attempt_id": attempt_id}
        clock = asyncio.get_running_loop().time
        deadline, executor = clock() + self.cancel_seconds, None
        try:
            async with self.journal.store.read() as tx:
                attempt = await tx.attempt(attempt_id)
            executor = self.journal.executors.get(attempt["command"]["payload"]["agent"]["runner"])
            # The request, a blocked write lock or pipe drain included, is bounded by the deadline.
            async with asyncio.timeout(self.cancel_seconds):
                if executor is not None:
                    await executor.cancel(attempt_id)
        except TimeoutError:
            log.warning("graceful cancellation request timed out", extra=evidence)
        except Exception:
            log.exception("graceful cancellation request failed", extra=evidence)
        try:
            while not task.done():
                if executor is not None and executor.fencing(attempt_id):
                    # Never cut a fence short; what follows it (progress, journaling) gets a fresh deadline.
                    deadline = clock() + self.cancel_seconds
                elif clock() >= deadline:
                    log.warning("graceful cancellation timed out; fencing", extra=evidence)
                    task.cancel()  # checked and cancelled in one step: no fence can have begun in between
                    break
                await asyncio.wait({task}, timeout=min(self.poll_seconds, max(deadline - clock(), 0)))
        finally:
            # Not confirmation: an unjournaled outcome is resolved by fencing in `run_once`.
            await asyncio.gather(task, return_exceptions=True)

    async def _resolve(self, attempt_id: str) -> None:
        """Fence an attempt whose outcome is uncertain; never launch it again."""
        journal = self.journal
        async with journal.store.read() as tx:
            attempt = await tx.attempt(attempt_id)
        if attempt is None or attempt["state"] not in INTERRUPTED:
            return
        executor = journal.executors.get(attempt["command"]["payload"]["agent"]["runner"])
        evidence = {"daemon.attempt_id": attempt_id, "daemon.state": attempt["state"]}
        try:
            fenced = executor is not None and await executor.fence(attempt)
            if fenced:
                async with journal.store.transaction() as tx:
                    await journal.finish(tx, await tx.attempt(attempt_id), "failed", {"error": {
                        "code": "PROVIDER_ERROR", "message": "Executor outcome was uncertain; it was fenced",
                        "retryable": False}, "reason": "executor_lost"}, "killed")
        except Exception:
            log.exception("fencing an uncertain attempt failed; ownership is kept", extra=evidence)
            fenced = False
        if fenced:
            self.backoff.done(attempt_id)
            log.info("uncertain attempt fenced", extra=evidence)
        else:
            self.backoff.failed(attempt_id)
            log.error("cannot confirm executor termination; attempt stays owned", extra=evidence)

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
        # Interrupted attempts stay launching/running/cancelling; the next start fences them.
        tasks = [task for task in (self._worker, *self.stopping.values(), *self.tasks.values()) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
