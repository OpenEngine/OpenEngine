"""Agent Runner capability, backed by the Codex CLI.

Runs `codex exec --json`, feeding the prompt on stdin and reading JSONL events
from stdout. The event vocabulary this parses is small and stable:

    {"type": "thread.started",  "thread_id": "019f..."}
    {"type": "turn.started"}
    {"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}
    {"type": "item.completed", "item": {"type": "command_execution",
                                        "command": "/bin/zsh -lc 'ls packages'",
                                        "aggregated_output": "...",
                                        "exit_code": 0, "status": "completed"}}
    {"type": "turn.completed", "usage": {"input_tokens": 15276,
                                         "cached_input_tokens": 9984, ...}}

Every item except the final message becomes an `AgentTurn.step`, so the
conversation records what the agent *did* and not merely what it concluded.
Unrecognised item types are recorded generically rather than dropped: a Codex
release that adds one should leave a gap in nobody's audit trail.

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

TODO(caching): we are effectively uncached, and the fix is not in this file.
----------------------------------------------------------------------------
Measured against codex-cli 0.144.4, `cached_input_tokens` comes back as exactly
9984 per model request no matter what we send -- one word or a whole
conversation, first turn or fifth:

    one-word question, 1 model call     15,276 in   9,984 cached
    question + one command, 2 calls     30,527 in  19,968 cached  (2 x 9,984)
    fifth turn of a real conversation   15,497 in   9,984 cached

That constant is Codex's own preamble -- sandbox rules, agent identity, plugin
and tool schemas, ~15k tokens of which ~10k caches. It is re-sent on every model
request, and a turn that runs two commands makes three of them. Nothing we
contribute is cached, and the dominant cost is not our transcript but Codex's
per-request overhead, which this adapter cannot influence at all.

Two things follow, and only the first is done:

* `render_prompt` is append-only, so turn N's prompt is a strict prefix of turn
  N+1's. That is a *precondition* for a cache hit, not a fix: our transcripts are
  a few hundred tokens, far below the ~1024-token block a cache entry is cut at,
  so there is nothing there to cache yet. It starts paying on long conversations,
  and only inside the cache's few-minute TTL, which a human chat routinely
  exceeds anyway.
* The real fix is `codex exec resume <thread_id>`: one growing prefix on the
  provider side instead of a fresh process rebuilding the preamble every turn.
  It needs somewhere on `AgentInstance` to keep the thread id, and a fallback to
  full replay when the rollout is gone -- without that the conversation stops
  being ours, which is the whole reason we hold it. `thread_id_of` already reads
  the id; nothing stores it yet.

Until then: assume every turn costs ~15k prompt tokens per model call it makes,
and do not read the growing transcript as the reason.
"""

import asyncio
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from engine.domain.agents import AgentProfile
from engine.domain.chat import Message, Role, ToolCall
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

#: Items that are not actions worth recording. Codex's reasoning items arrive
#: with their content stripped, and half a thought is worse in a transcript than
#: no thought at all.
IGNORED_ITEM_TYPES = frozenset({"reasoning"})

#: Where an item keeps its result, in preference order. Anything without one of
#: these is recorded as an action with an empty result rather than skipped.
OUTPUT_FIELDS = ("aggregated_output", "output", "result", "error")

#: Fields that describe the item rather than its arguments.
NON_ARGUMENT_FIELDS = frozenset({"id", "type", "status", *OUTPUT_FIELDS})

#: How much of a past step's output is replayed into a later prompt. The full
#: text is always stored; this only bounds what a later turn re-reads, because
#: an 8KB file dump from three questions ago is billed again every turn and
#: rarely earns it.
MAX_REPLAYED_OUTPUT_CHARS = 1000


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


def render_message(message: Message) -> str:
    """One conversation entry as a stable block of text.

    Stable is the operative word: the same message must render identically
    whenever it appears, or the append-only property below is lost.
    """
    if message.tool_calls:
        return "\n\n".join(
            f"{ROLE_LABELS[message.role]} ran {call.name}: {call.arguments}"
            for call in message.tool_calls
        )
    body = message.content.strip()
    if message.role is Role.TOOL and len(body) > MAX_REPLAYED_OUTPUT_CHARS:
        omitted = len(body) - MAX_REPLAYED_OUTPUT_CHARS
        body = f"{body[:MAX_REPLAYED_OUTPUT_CHARS]}\n… ({omitted} more characters, stored in full)"
    return f"{ROLE_LABELS[message.role]}: {body}"


def render_prompt(profile: AgentProfile, messages: Sequence[Message]) -> str:
    """Flatten a profile and a conversation into one prompt.

    Codex takes a single block of text, so the structure a chat API carries in
    roles has to be spelled out.

    **Append-only.** Every message renders to a fixed block and the blocks are
    joined in order, so the prompt for turn N is a strict prefix of the prompt
    for turn N+1. Prompt caches match on prefixes, and an earlier version of this
    function moved the latest message between two headings each turn, which broke
    the prefix one line after the instructions and made a cache hit impossible.
    Nothing may be inserted, reordered, or reworded after the fact -- including
    any trailing "now answer this" instruction, which is why there isn't one.
    """
    if not messages:
        raise ValueError("cannot run a turn with no messages")

    sections: list[str] = []
    if profile.instructions.strip():
        sections.append(f"# Your instructions\n\n{profile.instructions.strip()}")

    rendered = [render_message(m) for m in messages if m.content.strip() or m.tool_calls]
    sections.append("# Conversation\n\n" + "\n\n".join(rendered))
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


def action_messages(item: dict[str, Any], call_id: str) -> tuple[Message, Message]:
    """One completed action as the pair of messages that records it.

    An assistant message carrying the call, then the tool message carrying its
    result -- the same shape a chat API would have produced had it asked us to
    run the thing. The arguments are whatever the item said minus its bookkeeping
    fields, so an item type this adapter has never heard of still records what
    the agent did with it.
    """
    arguments = {k: v for k, v in item.items() if k not in NON_ARGUMENT_FIELDS}
    call = ToolCall(
        call_id=call_id,
        name=str(item.get("type", "action")),
        arguments=json.dumps(arguments, sort_keys=True),
    )
    output = next(
        (str(item[field]) for field in OUTPUT_FIELDS if item.get(field) not in (None, "")),
        "",
    )
    exit_code = item.get("exit_code")
    if exit_code is not None:
        output = f"{output}\n(exit {exit_code})".strip()
    return Message.assistant(tool_calls=(call,)), Message.tool_result(call_id, output)


def turn_from_events(events: Iterable[dict[str, Any]]) -> AgentTurn:
    """Assemble the answer, and everything the agent did to reach it.

    The last `agent_message` is the answer. Every earlier message is narration
    and every other item is an action, and both are steps -- so the conversation
    ends up holding the commands and their output, not just the conclusion.
    """
    events = tuple(events)
    thread = thread_id_of(events) or "codex"
    entries: list[tuple[str, Any]] = []
    usage: TokenUsage | None = None
    failed = False

    for index, event in enumerate(events):
        match event.get("type"):
            case "item.completed":
                item = event.get("item") or {}
                kind = item.get("type")
                if kind in IGNORED_ITEM_TYPES:
                    continue
                if kind == "agent_message":
                    if item.get("text"):
                        entries.append(("message", str(item["text"])))
                else:
                    call_id = f"{thread}:{item.get('id') or f'item-{index}'}"
                    entries.append(("action", action_messages(item, call_id)))
            case "turn.completed":
                reported = event.get("usage") or {}
                usage = TokenUsage(
                    prompt_tokens=int(reported.get("input_tokens", 0)),
                    completion_tokens=int(reported.get("output_tokens", 0)),
                    cached_prompt_tokens=int(reported.get("cached_input_tokens", 0)),
                )
            case "turn.failed" | "error":
                failed = True

    spoken = [index for index, (kind, _) in enumerate(entries) if kind == "message"]
    answer_at = spoken[-1] if spoken else None

    steps: list[Message] = []
    for index, (kind, payload) in enumerate(entries):
        if index == answer_at:
            continue
        if kind == "message":
            steps.append(Message.assistant(payload))
        else:
            steps.extend(payload)

    if answer_at is None:
        if failed:
            return AgentTurn(
                message=Message.assistant("Codex reported a failed turn"),
                finish_reason=FinishReason.ERROR,
                usage=usage,
                steps=tuple(steps),
            )
        raise CodexExecutionError("Codex produced no agent message")

    return AgentTurn(
        message=Message.assistant(entries[answer_at][1]),
        finish_reason=FinishReason.ERROR if failed else FinishReason.STOP,
        usage=usage,
        steps=tuple(steps),
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
    "action_messages",
    "parse_events",
    "render_message",
    "render_prompt",
    "thread_id_of",
    "turn_from_events",
]
