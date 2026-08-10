"""Agent Runner capability, backed by the Codex CLI.

Runs `codex exec --json`, feeding the prompt on stdin and reading JSONL events
from stdout. The event vocabulary this parses is small and stable:

    {"type": "thread.started",  "thread_id": "019f..."}
    {"type": "turn.started"}
    {"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}
    {"type": "turn.completed", "usage": {"input_tokens": 15276, ...}}

Two consequences of Codex being an *agent* rather than a chat completion, both
of which this adapter is deliberately loud about rather than papering over:

* **It owns its own tools.** Codex decides when to read a file or run a command
  and does it internally; there is no way to hand it our `ToolSpec`s and get
  tool calls back. So a profile with grants cannot run here -- see
  `CodexToolsUnsupportedError`. Bridging the two means exposing our tools to
  Codex over MCP (`codex mcp`), which is a ticket, not a flag.

* **It is stateless here.** Codex can resume its own sessions by `thread_id`,
  but our conversation is the source of truth, so each turn sends the whole
  transcript and takes whatever comes back. That costs prompt tokens and loses
  Codex's own intermediate reasoning between turns. Threading `thread_id`
  through as an adapter-side optimisation needs somewhere on `AgentInstance` to
  keep it, and is worth doing only once conversations outlive a process.

Stdlib only: `asyncio.create_subprocess_exec` and `json`. No SDK.
"""

import asyncio
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from engine.domain.agents import AgentProfile
from engine.domain.chat import Message, Role
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import AgentTurn, FinishReason, TokenUsage

#: How each role is labelled when a conversation is flattened into one prompt.
ROLE_LABELS: Mapping[Role, str] = {
    Role.SYSTEM: "System",
    Role.USER: "User",
    Role.ASSISTANT: "Assistant",
    Role.TOOL: "Tool result",
}

#: Sandbox policies the CLI accepts. Chat defaults to the read-only one: an
#: agent you are talking to should not be able to edit the tree as a side
#: effect of answering a question.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")


class CodexUnavailableError(RuntimeError):
    """The `codex` binary is not on PATH."""


class CodexExecutionError(RuntimeError):
    """Codex ran and failed, timed out, or produced no answer."""


class CodexToolsUnsupportedError(NotImplementedError):
    """A profile granted tools that Codex cannot be offered.

    Raised rather than ignored: dropping the grants would leave an agent quietly
    less capable than its profile promises, and the caller with no way to know.
    """

    def __init__(self, tool_names: Sequence[str]) -> None:
        super().__init__(
            f"Codex runs its own tools and cannot be offered {list(tool_names)}; "
            "exposing engine tools to Codex over MCP lands with the tools ticket"
        )
        self.tool_names = tuple(tool_names)


def render_prompt(profile: AgentProfile, messages: Sequence[Message]) -> str:
    """Flatten a profile and a conversation into one prompt.

    Codex takes a single block of text, so the structure a chat API would carry
    in roles has to be spelled out. Prior turns become a labelled transcript and
    the latest message is set apart, so the model can tell what it is answering
    from what it is merely remembering.
    """
    if not messages:
        raise ValueError("cannot run a turn with no messages")

    sections: list[str] = []
    if profile.instructions.strip():
        sections.append(f"# Your instructions\n\n{profile.instructions.strip()}")

    *prior, latest = messages
    if prior:
        transcript = "\n\n".join(
            f"{ROLE_LABELS[m.role]}: {m.content}".strip() for m in prior if m.content.strip()
        )
        if transcript:
            sections.append(f"# Conversation so far\n\n{transcript}")

    if latest.role is Role.USER:
        sections.append(f"# Message to answer\n\n{latest.content.strip()}")
    else:
        # Not a user message -- the caller wants the conversation continued
        # rather than a reply to anything in particular.
        sections.append(
            f"# Continue the conversation\n\n"
            f"{ROLE_LABELS[latest.role]}: {latest.content}".strip()
        )
    return "\n\n".join(sections)


def parse_events(stdout: str) -> tuple[dict[str, Any], ...]:
    """JSONL to dicts, skipping anything that is not a JSON object.

    Tolerant on purpose: the CLI writes progress and warnings around the event
    stream, and a stray line is not a reason to lose an answer that arrived.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return tuple(events)


def turn_from_events(events: Iterable[dict[str, Any]]) -> AgentTurn:
    """Assemble the assistant's answer out of Codex's event stream."""
    texts: list[str] = []
    usage: TokenUsage | None = None
    failed = False

    for event in events:
        match event.get("type"):
            case "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    texts.append(str(item["text"]))
            case "turn.completed":
                reported = event.get("usage") or {}
                usage = TokenUsage(
                    prompt_tokens=int(reported.get("input_tokens", 0)),
                    completion_tokens=int(reported.get("output_tokens", 0)),
                )
            case "turn.failed" | "error":
                failed = True

    if failed:
        detail = "\n\n".join(texts) or "Codex reported a failed turn"
        return AgentTurn(
            message=Message.assistant(detail),
            finish_reason=FinishReason.ERROR,
            usage=usage,
        )
    if not texts:
        raise CodexExecutionError("Codex produced no agent message")

    return AgentTurn(
        message=Message.assistant("\n\n".join(texts)),
        finish_reason=FinishReason.STOP,
        usage=usage,
    )


def thread_id_of(events: Iterable[dict[str, Any]]) -> str | None:
    """Codex's own session id, if it announced one.

    Not used yet. It is what a future `codex exec resume` would need, and it is
    cheaper to read here than to re-derive later.
    """
    for event in events:
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


class CodexAgentRunner:
    """Runs an agent turn by shelling out to the Codex CLI.

    Implements `engine.ports.AgentRunner`.
    """

    def __init__(
        self,
        binary_path: str = "codex",
        timeout_seconds: float = 600.0,
        sandbox: str = "read-only",
        working_directory: str = ".",
        model: str = "",
    ) -> None:
        if sandbox not in SANDBOX_MODES:
            raise ValueError(f"sandbox must be one of {SANDBOX_MODES}, got {sandbox!r}")
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds
        self._sandbox = sandbox
        self._working_directory = working_directory
        self._model = model
        #: Live processes, so `cancel` has something to reach for.
        self._running: dict[AgentRunId, asyncio.subprocess.Process] = {}

    def command_line(self, profile: AgentProfile) -> list[str]:
        """The argv this runner would use. Public so the wiring is inspectable
        without running anything."""
        argv = [
            self._binary_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            self._sandbox,
            "-C",
            self._working_directory,
        ]
        model = profile.model or self._model
        if model:
            argv += ["--model", model]
        return argv

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        if tools:
            raise CodexToolsUnsupportedError([tool.name for tool in tools])
        if workspace_id is not None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        if shutil.which(self._binary_path) is None:
            raise CodexUnavailableError(
                f"{self._binary_path!r} is not on PATH -- install the Codex CLI, "
                "or point the runner at the binary"
            )

        prompt = render_prompt(profile, messages)
        process = await asyncio.create_subprocess_exec(
            *self.command_line(profile),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running[agent_run_id] = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode()), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError as timeout:
            process.kill()
            await process.wait()
            raise CodexExecutionError(
                f"Codex did not finish within {self._timeout_seconds:.0f}s"
            ) from timeout
        finally:
            self._running.pop(agent_run_id, None)

        if process.returncode != 0:
            raise CodexExecutionError(
                f"codex exited {process.returncode}: {_tail(stderr.decode(errors='replace'))}"
            )
        return turn_from_events(parse_events(stdout.decode(errors="replace")))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        """Terminate the run if it is still going. Safe to call otherwise."""
        process = self._running.get(agent_run_id)
        if process is None or process.returncode is not None:
            return
        process.terminate()


def _tail(text: str, lines: int = 5) -> str:
    """The last few lines of stderr -- enough to diagnose, short enough to read.

    Codex writes warnings there on healthy runs, so the whole stream would bury
    the actual error.
    """
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:]) if kept else "(no stderr)"


__all__ = [
    "CodexAgentRunner",
    "CodexExecutionError",
    "CodexToolsUnsupportedError",
    "CodexUnavailableError",
    "parse_events",
    "render_prompt",
    "thread_id_of",
    "turn_from_events",
]
