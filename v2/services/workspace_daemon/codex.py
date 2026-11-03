"""The opt-in `codex` executor: one operator-configured ACP adapter, run read-only under supervision.

The operator configures the adapter's absolute path and the supervisor; a
command selects neither, and Codex accepts no task settings. Launch revalidates
the task's context inside the workspace root, starts the adapter under the
launch identity the worker already journaled, and opens (`session/new`) or
loads (`session/load`) the conversation in read-only mode with the requested
model if the adapter offers it. Wait sends one prompt, journals agent message
chunks as `conversation` progress and the turn's explicit token usage as one
`usage` delta, then fences the whole process tree before reporting an outcome.
Cumulative `usage_update` notifications are not deltas and are ignored; no
cost or artifact is ever reported. Cancel sends `session/cancel` during a
prompt, or skips a prompt not sent yet; the worker bounds it and then fences.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from workspace_daemon import context as task_context
from workspace_daemon.acp import MAX_FRAME, ACPClient, ACPError
from workspace_daemon.executor import LaunchFailed, Outcome, Progress
from workspace_daemon.journal import now
from workspace_daemon.launch import ENVIRONMENT

log = logging.getLogger(__name__)

MAX_PROMPT_BYTES = 524288
CHUNK = 32000
MAX_SUMMARY = 65536
USAGE = {"inputTokens": "input_tokens", "outputTokens": "output_tokens", "cachedReadTokens": "cache_read_tokens"}


class Supervisor(Protocol):
    def plan(self) -> dict[str, Any]: ...

    def command(self, handle: dict[str, Any], argv: list[str]) -> list[str]: ...

    async def fence(self, handle: Any) -> bool: ...


class UnconfirmedTermination(RuntimeError):
    """The process tree could not be confirmed empty; the attempt must stay owned."""

    def __init__(self) -> None:
        super().__init__("the executor process tree is not confirmed empty")


def prompt_text(root: Path, instructions: list[Path], task: dict[str, Any]) -> str:
    sections = [f"Workspace instructions ({path.relative_to(root)}):\n{task_context.read_instruction(root, path)}"
                for path in instructions]
    sections.append("Task:\n" + task["instruction"])
    if task.get("inputs"):
        sections.append("Task inputs:\n" + json.dumps(task["inputs"], ensure_ascii=False))
    text = "\n\n".join(sections)
    if len(text.encode()) > MAX_PROMPT_BYTES:
        raise ValueError(len(text.encode()))  # the assembled context is over the limit
    return text


class CodexExecutor:
    progress_kinds: tuple[str, ...] = ("conversation", "usage")
    conversational = True

    def __init__(self, executable: Path, supervisor: Supervisor, *, prompt_timeout: float = 3600.0) -> None:
        if not executable.is_absolute():
            raise ValueError(executable)  # must be an absolute, operator-configured path
        self.executable, self.supervisor, self.prompt_timeout = executable, supervisor, prompt_timeout
        self.spawn = asyncio.create_subprocess_exec
        self.running: dict[str, tuple[ACPClient, str]] = {}
        self.prompting: dict[str, ACPClient] = {}
        self.cancelled: set[str] = set()
        self.fences: set[str] = set()

    def validate(self, payload: dict[str, Any]) -> None:
        if payload["agent"].get("settings"):
            raise ValueError(payload["agent"]["settings"])  # configured by the operator, not the task

    def plan(self, attempt_id: str) -> dict[str, Any]:
        return self.supervisor.plan()

    async def launch(self, attempt_id: str, command: dict[str, Any], root: Path, handle: dict[str, Any],
                     session: dict[str, Any] | None) -> dict[str, Any]:
        payload = command["payload"]
        previous = None
        if payload["session_mode"] == "continue":
            # Never replace a conversation that cannot be loaded with a new one.
            previous = session and session["executor_resumable"] and session["executor_session_id"]
            if not previous:
                raise LaunchFailed(LookupError(command["target"]["session_id"]))
        try:
            cwd, instructions = task_context.resolve(root, payload["context"])
            prompt = prompt_text(root, instructions, payload["task"])
        except (OSError, ValueError) as error:
            raise LaunchFailed(error) from error
        client = process = None
        try:
            argv = self.supervisor.command(handle, [str(self.executable)])
            process = await self.spawn(*argv, cwd=cwd, env=ENVIRONMENT, stdin=asyncio.subprocess.PIPE,
                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME + 1)
            client = ACPClient(process)
            session_id, resumable = await client.open(cwd, previous=previous, model=payload["agent"].get("model"))
        except BaseException as error:
            # A cancelled launch fences too, before the cancellation propagates.
            if not await self._stop(handle, client, process):
                raise UnconfirmedTermination from error
            if not isinstance(error, Exception):
                raise
            raise LaunchFailed(error, "never_started" if process is None else "killed") from error
        self.running[attempt_id] = (client, prompt)
        return {"executor_session_id": session_id, "resumable": resumable}

    async def wait(self, attempt_id: str, handle: dict[str, Any], progress: Progress) -> Outcome:
        client, prompt = self.running.pop(attempt_id)
        self.prompting[attempt_id] = client
        pending, summary = "", ""

        async def flush(size: int) -> None:
            # Streamed fragments are coalesced: one message per CHUNK characters and one for the rest of the turn.
            nonlocal pending
            while pending and len(pending) >= size:
                await progress({"kind": "conversation", "message_id": "msg_" + uuid4().hex, "role": "agent",
                                "text": pending[:CHUNK]})
                pending = pending[CHUNK:]

        async def update(value: dict[str, Any]) -> None:
            nonlocal pending, summary
            content = value.get("content")
            if (value.get("sessionUpdate") != "agent_message_chunk" or not isinstance(content, dict)
                    or content.get("type") != "text" or not isinstance(content.get("text"), str)):
                return
            pending += content["text"]
            summary = (summary + content["text"])[-MAX_SUMMARY:]
            await flush(CHUNK)

        outcome: Outcome = ("failed", {"error": {"code": "PROVIDER_ERROR", "message": "Agent did not complete its turn",
                                                 "retryable": False}, "reason": "executor_failed"}, "killed")
        reported: dict[str, Any] | None = None
        try:
            if attempt_id in self.cancelled:
                return outcome  # cancelled while launching: never prompted, and fenced below first
            started = now()
            result = await client.prompt(prompt, update, self.prompt_timeout)
            usage = result.get("usage") or {}
            if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("inputTokens", "outputTokens")):
                reported = {"kind": "usage", "period_start": started, "period_end": now(), "usage": {
                    name: usage[key] for key, name in USAGE.items() if type(usage.get(key)) is int and usage[key] >= 0}}
            if result.get("stopReason") == "end_turn":
                outcome = ("completed", {"result": {"summary": summary.strip() or "Agent completed the task."}},
                           "exited")
        except (ACPError, TimeoutError, OSError) as error:
            log.warning("ACP prompt failed: %s", type(error).__name__, extra={"daemon.attempt_id": attempt_id})
        except Exception:
            # Once the tree is fenced this is a known failure, not a lost executor.
            log.exception("ACP prompt failed unexpectedly", extra={"daemon.attempt_id": attempt_id})
        finally:
            self.prompting.pop(attempt_id, None)
            self.cancelled.discard(attempt_id)
            # A finished turn is not evidence that the agent's tools stopped.
            # Cancellation still fences here, and then skips the journaling below.
            self.fences.add(attempt_id)  # the cancel deadline never interrupts this fence
            try:
                fenced = await self._stop(handle, client, client.process)
            finally:
                self.fences.discard(attempt_id)
            if not fenced:
                raise UnconfirmedTermination
        # Only once the tree is fenced may journaling wait for outbox capacity: the agent cannot outlive its deadline.
        # A cancel deadline may still interrupt that wait; the attempt is then fenced again and recorded.
        try:
            await flush(1)  # text the agent already streamed is kept even if the turn failed
            if reported is not None:
                await progress(reported)
        except Exception:
            # Never report success with conversation output or usage permanently missing.
            log.exception("final ACP progress was not journaled", extra={"daemon.attempt_id": attempt_id})
            return ("failed", {"error": {"code": "PROVIDER_ERROR", "message": "Agent progress was not journaled",
                                         "retryable": False}, "reason": "executor_failed"}, "killed")
        return outcome

    async def cancel(self, attempt_id: str) -> None:
        self.cancelled.add(attempt_id)
        if (client := self.prompting.get(attempt_id)) is not None:
            await client.cancel()

    def fencing(self, attempt_id: str) -> bool:
        return attempt_id in self.fences

    async def fence(self, attempt: dict[str, Any]) -> bool:
        attempt_id = attempt["command"]["target"]["attempt_id"]
        self.cancelled.discard(attempt_id)
        client = self.running.pop(attempt_id, (None, ""))[0]
        return await self._stop(attempt["executor_handle"], client, client and client.process)

    async def _stop(self, handle: Any, client: ACPClient | None, process) -> bool:
        if not await self.supervisor.fence(handle):
            return False
        if client is not None:
            await client.close()
        elif process is not None:
            try:
                async with asyncio.timeout(5):
                    await process.communicate()  # closes stdin and drains the pipes so the process is reaped
            except (OSError, TimeoutError):
                log.warning("adapter pipes did not close after fencing")
        return True
