"""Agent Runner capability, backed by the Claude Code CLI.

Runs `claude -p --output-format stream-json --verbose`, prompt on stdin, JSONL
events on stdout. The records this parses:

    {"type":"system","subtype":"init","session_id":"4aeecd85-…","tools":[…]}
    {"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_…",
                                               "name":"Glob","input":{…}}]}}
    {"type":"user","message":{"content":[{"type":"tool_result",
                                          "tool_use_id":"toolu_…","content":"…"}]}}
    {"type":"assistant","message":{"content":[{"type":"text","text":"…"}]}}
    {"type":"result","subtype":"success","is_error":false,"result":"…",
     "usage":{"input_tokens":4,"cache_creation_input_tokens":6478,
              "cache_read_input_tokens":41156,"output_tokens":149},
     "total_cost_usd":0.0897}

Two differences from the Codex adapter, both in this one's favour:

* **Tool calls arrive structured.** `tool_use` and `tool_result` blocks carry
  ids, so pairing a call with its result is reading a field rather than
  inferring from order.
* **Caching actually happens.** Claude Code reports `cache_read_input_tokens`
  around 41k against 4 raw input tokens -- roughly 86% of the prompt served from
  cache, where `codex exec` sits at 65% and pins there regardless of what we
  send. Worth knowing when reading the `TODO(caching)` note in the Codex
  adapter: that problem is `codex exec` rebuilding a process per turn, not
  something inherent to driving a CLI.

Like Codex, Claude Code brings its own tools and cannot be handed ours, so a
profile with grants is refused rather than quietly run without them. What it can
be told is *which* of its own tools to use, via `allowed_tools` -- the read-only
default is this adapter's equivalent of Codex's read-only sandbox.

Stdlib only: a subprocess and a JSON parser.
"""

import asyncio
import json
import shutil
from collections.abc import Iterable, Sequence
from typing import Any

from engine.domain.agents import AgentProfile
from engine.domain.chat import Message, ToolCall
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import AgentTurn, FinishReason, TokenUsage
from engine.runtime.transcript import flatten

#: Claude Code's own tools that only read. The default, because an agent you are
#: talking to should not edit the tree as a side effect of answering. Anything
#: outside this list is denied by the CLI rather than silently allowed.
READ_ONLY_TOOLS = ("Read", "Glob", "Grep")

#: Content blocks that are not worth recording. Thinking blocks are the model's
#: working, not the conversation's.
IGNORED_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


class ClaudeUnavailableError(RuntimeError):
    """The `claude` binary is not on PATH."""


class ClaudeExecutionError(RuntimeError):
    """Claude Code ran and failed, timed out, or produced no answer."""


class ClaudeToolsUnsupportedError(NotImplementedError):
    """A profile granted tools that Claude Code cannot be offered.

    Raised rather than ignored, for the same reason as the Codex adapter: an
    agent quietly less capable than its profile promises is worse than one that
    refuses to start. Reaching our tools from here means exposing them over MCP.
    """

    def __init__(self, tool_names: Sequence[str]) -> None:
        super().__init__(
            f"Claude Code runs its own tools and cannot be offered {list(tool_names)}; "
            "exposing engine tools to it over MCP lands with the tools ticket"
        )
        self.tool_names = tuple(tool_names)


def parse_events(stdout: str) -> tuple[dict[str, Any], ...]:
    """JSONL to dicts, skipping anything that is not a JSON object."""
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


def session_id_of(events: Iterable[dict[str, Any]]) -> str | None:
    """Claude Code's own session id, if it announced one.

    Not used yet; it is what `claude --resume` would need.
    """
    for event in events:
        if event.get("session_id"):
            return str(event["session_id"])
    return None


def _block_text(content: Any) -> str:
    """A tool result's content, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part) or json.dumps(content, sort_keys=True)
    return "" if content is None else json.dumps(content, sort_keys=True)


def _usage_of(event: dict[str, Any]) -> TokenUsage:
    """Claude reports fresh, cache-written and cache-read input separately.

    They are summed into `prompt_tokens` so the number means the same thing it
    does for any other runner: everything the model was given.
    """
    reported = event.get("usage") or {}
    fresh = int(reported.get("input_tokens", 0))
    written = int(reported.get("cache_creation_input_tokens", 0))
    read = int(reported.get("cache_read_input_tokens", 0))
    cost = event.get("total_cost_usd")
    return TokenUsage(
        prompt_tokens=fresh + written + read,
        completion_tokens=int(reported.get("output_tokens", 0)),
        cached_prompt_tokens=read,
        cost_usd=float(cost) if cost is not None else None,
    )


def turn_from_events(events: Iterable[dict[str, Any]]) -> AgentTurn:
    """Assemble the answer, and everything the agent did to reach it.

    The last text block is the answer; every earlier one is narration, and every
    tool call and result is recorded in between. The `result` record's own text
    is used only as a fallback, so a turn that ends in a tool loop still reports
    something rather than raising.
    """
    entries: list[tuple[str, Any]] = []
    usage: TokenUsage | None = None
    failed = False
    reported_answer: str | None = None

    for event in events:
        match event.get("type"):
            case "assistant":
                for block in (event.get("message") or {}).get("content") or ():
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind in IGNORED_BLOCK_TYPES:
                        continue
                    if kind == "text" and str(block.get("text", "")).strip():
                        entries.append(("message", str(block["text"])))
                    elif kind == "tool_use":
                        entries.append(
                            (
                                "call",
                                ToolCall(
                                    call_id=str(block.get("id", "")),
                                    name=str(block.get("name", "tool")),
                                    arguments=json.dumps(block.get("input") or {}, sort_keys=True),
                                ),
                            )
                        )
            case "user":
                for block in (event.get("message") or {}).get("content") or ():
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    text = _block_text(block.get("content"))
                    if block.get("is_error"):
                        text = f"error: {text}"
                    entries.append(("result", (str(block.get("tool_use_id", "")), text)))
            case "result":
                usage = _usage_of(event)
                failed = bool(event.get("is_error"))
                if event.get("result"):
                    reported_answer = str(event["result"])

    spoken = [index for index, (kind, _) in enumerate(entries) if kind == "message"]
    answer_at = spoken[-1] if spoken else None

    steps: list[Message] = []
    for index, (kind, payload) in enumerate(entries):
        if index == answer_at:
            continue
        match kind:
            case "message":
                steps.append(Message.assistant(payload))
            case "call":
                steps.append(Message.assistant(tool_calls=(payload,)))
            case "result":
                call_id, text = payload
                steps.append(Message.tool_result(call_id, text))

    answer = entries[answer_at][1] if answer_at is not None else reported_answer
    if answer is None:
        raise ClaudeExecutionError("Claude Code produced no assistant message")

    return AgentTurn(
        message=Message.assistant(answer),
        finish_reason=FinishReason.ERROR if failed else FinishReason.STOP,
        usage=usage,
        steps=tuple(steps),
    )


class ClaudeCodeAgentRunner:
    """Runs an agent turn by shelling out to the Claude Code CLI.

    Implements `engine.ports.AgentRunner`.
    """

    def __init__(
        self,
        binary_path: str = "claude",
        timeout_seconds: float = 600.0,
        allowed_tools: Sequence[str] = READ_ONLY_TOOLS,
        working_directory: str = ".",
        model: str = "",
    ) -> None:
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds
        self._allowed_tools = tuple(allowed_tools)
        self._working_directory = working_directory
        self._model = model
        self._running: dict[AgentRunId, asyncio.subprocess.Process] = {}

    def command_line(self, profile: AgentProfile) -> list[str]:
        """The argv this runner would use. Public so the wiring is inspectable
        without running anything."""
        argv = [self._binary_path, "-p", "--output-format", "stream-json", "--verbose"]
        if profile.instructions.strip():
            # A real system-prompt channel, unlike `codex exec` -- so the
            # instructions never enter the conversation text.
            argv += ["--append-system-prompt", profile.instructions.strip()]
        if self._allowed_tools:
            # Variadic, and an empty list would swallow the next flag.
            argv += ["--allowedTools", *self._allowed_tools]
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
            raise ClaudeToolsUnsupportedError([tool.name for tool in tools])
        if workspace_id is not None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        if shutil.which(self._binary_path) is None:
            raise ClaudeUnavailableError(
                f"{self._binary_path!r} is not on PATH -- install the Claude Code CLI, "
                "or point the runner at the binary"
            )

        process = await asyncio.create_subprocess_exec(
            *self.command_line(profile),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._working_directory,
        )
        self._running[agent_run_id] = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(flatten(messages).encode()),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as timeout:
            process.kill()
            await process.wait()
            raise ClaudeExecutionError(
                f"Claude Code did not finish within {self._timeout_seconds:.0f}s"
            ) from timeout
        finally:
            self._running.pop(agent_run_id, None)

        if process.returncode != 0:
            raise ClaudeExecutionError(
                f"claude exited {process.returncode}: "
                f"{_tail(stderr.decode(errors='replace'))}"
            )
        return turn_from_events(parse_events(stdout.decode(errors="replace")))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        """Terminate the run if it is still going. Safe to call otherwise."""
        process = self._running.get(agent_run_id)
        if process is None or process.returncode is not None:
            return
        process.terminate()


def _tail(text: str, lines: int = 5) -> str:
    """The last few lines of stderr -- enough to diagnose, short enough to read."""
    kept = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(kept[-lines:]) if kept else "(no stderr)"


__all__ = [
    "READ_ONLY_TOOLS",
    "ClaudeCodeAgentRunner",
    "ClaudeExecutionError",
    "ClaudeToolsUnsupportedError",
    "ClaudeUnavailableError",
    "parse_events",
    "session_id_of",
    "turn_from_events",
]
