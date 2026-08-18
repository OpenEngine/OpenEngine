"""Agent Runner capability, backed by the Codex CLI.

``CodexAgentRunner`` drives two Codex transports: `codex exec --json` for a
turn that runs start to finish, and app-server's stdio JSON-RPC for one that may
pause on an approval request and resume where it stopped. Which is used is the
caller's choice of method, not a choice of runner. The event vocabulary this
package parses is small and stable:

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
  `CodexToolsUnsupportedError`. Workflow runs are the narrow exception: the
  runtime attaches its run-bound `complete_step` and `fail_step` MCP server.

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
import os
import shutil
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

from engine.adapters.agent_runner.codex.permissions import (
    CODEX_PERMISSION_TRANSLATOR,
    CodexPermissionTranslator,
)
from engine.domain.agents import AgentProfile
from engine.domain.chat import Message, ToolCall
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import (
    AgentTurn,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalKind,
    ApprovalRequest,
    FinishReason,
    McpServerConfig,
    TokenUsage,
    TurnObserver,
)
from engine.ports.workspace_provider import WorkspaceProvider
from engine.runtime.streams import read_lines
from engine.runtime.transcript import flatten

#: Sandbox policies the CLI accepts. Chat defaults to the read-only one: an
#: agent you are talking to should not be able to edit the tree as a side
#: effect of answering a question.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

#: What an interactive turn asks app-server for. Codex runs what its sandbox
#: allows and asks before anything that would escape it -- which is what makes
#: a writable sandbox safe to pair with: the approvals are the boundary, not
#: the sandbox alone.
INTERACTIVE_APPROVAL_POLICY = "on-request"

#: Items that are not actions worth recording. Codex's reasoning items arrive
#: with their content stripped, and half a thought is worse in a transcript than
#: no thought at all.
IGNORED_ITEM_TYPES = frozenset({"reasoning"})

#: App-server uses camelCase names for the same concepts emitted by
#: ``codex exec --json``. Keep this translation at the transport boundary so
#: the stored transcript does not depend on which Codex transport ran it.
APP_SERVER_ITEM_TYPES = {
    "agentMessage": "agent_message",
    "commandExecution": "command_execution",
    "fileChange": "file_change",
}

APP_SERVER_ITEM_FIELDS = {
    "aggregatedOutput": "aggregated_output",
    "exitCode": "exit_code",
    "commandActions": "command_actions",
    "durationMs": "duration_ms",
}

APP_SERVER_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": ApprovalKind.COMMAND_EXECUTION,
    "item/fileChange/requestApproval": ApprovalKind.FILE_CHANGE,
}

APP_SERVER_DECISIONS = {
    ApprovalDecision.ACCEPT: "accept",
    ApprovalDecision.ACCEPT_FOR_SESSION: "acceptForSession",
    ApprovalDecision.CANCEL: "cancel",
}

#: Approval params that address the request rather than describe the action.
#: Everything else -- the command's parsed actions, a file change's per-path
#: edits, whatever a later release adds -- is carried through as the request's
#: arguments, because what a person is being asked to allow is not something to
#: drop on the way to them.
APP_SERVER_APPROVAL_ENVELOPE = frozenset(
    {"threadId", "turnId", "itemId", "reason", "command", "cwd", "availableDecisions"}
)

#: Where an item keeps its result, in preference order. Anything without one of
#: these is recorded as an action with an empty result rather than skipped.
OUTPUT_FIELDS = ("aggregated_output", "output", "result", "error")

#: Fields that describe the item rather than its arguments.
NON_ARGUMENT_FIELDS = frozenset({"id", "type", "status", "exit_code", *OUTPUT_FIELDS})


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
            "only the runtime-bound workflow terminal tools are available over MCP"
        )
        self.tool_names = tuple(tool_names)


def render_prompt(profile: AgentProfile, messages: Sequence[Message]) -> str:
    """Flatten a profile and a conversation into one prompt.

    `codex exec` has no channel for a system prompt, so the instructions go in
    ahead of the conversation. They are first and never change, which keeps the
    whole prompt append-only -- see `engine.runtime.transcript`, which owns that
    invariant and the rendering that maintains it.
    """
    if not messages:
        raise ValueError("cannot run a turn with no messages")

    sections: list[str] = []
    if profile.instructions.strip():
        sections.append(f"# Your instructions\n\n{profile.instructions.strip()}")
    sections.append(flatten(messages))
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


def app_server_sandbox_policy(sandbox: str) -> dict[str, Any]:
    """Translate the stable CLI sandbox names to app-server's v2 schema."""
    match sandbox:
        case "read-only":
            return {"type": "readOnly"}
        case "workspace-write":
            return {"type": "workspaceWrite"}
        case "danger-full-access":
            return {"type": "dangerFullAccess"}
        case _:  # Construction validates this; keep the helper safe on its own.
            raise ValueError(f"sandbox must be one of {SANDBOX_MODES}, got {sandbox!r}")


def approval_request_from_app_server(message: dict[str, Any]) -> ApprovalRequest | None:
    """Normalize one server-initiated app-server approval request.

    Unknown server requests are deliberately not approvals. The transport
    rejects them instead of guessing a response shape and accidentally granting
    a capability introduced by a future Codex release.
    """
    method = message.get("method")
    kind = APP_SERVER_APPROVAL_METHODS.get(str(method))
    if kind is None or "id" not in message:
        return None

    params = message.get("params") or {}
    if not isinstance(params, dict):
        return None
    thread_id = str(params.get("threadId", ""))
    turn_id = str(params.get("turnId", ""))
    request_id = str(message["id"])

    available = params.get("availableDecisions")
    if isinstance(available, list):
        by_wire = {wire: decision for decision, wire in APP_SERVER_DECISIONS.items()}
        allowed = tuple(
            by_wire[value]
            for value in available
            if isinstance(value, str) and value in by_wire
        )
    else:
        allowed = tuple(APP_SERVER_DECISIONS)

    command = params.get("command")
    cwd = params.get("cwd")
    details = {
        key: value
        for key, value in params.items()
        if key not in APP_SERVER_APPROVAL_ENVELOPE
    }
    return ApprovalRequest(
        approval_id=":".join(part for part in (thread_id, turn_id, request_id) if part),
        kind=kind,
        reason=str(params["reason"]) if params.get("reason") is not None else None,
        command=str(command) if command is not None else None,
        cwd=str(cwd) if cwd is not None else None,
        tool_name=(
            "command_execution" if kind is ApprovalKind.COMMAND_EXECUTION else "file_change"
        ),
        arguments=json.dumps(details, sort_keys=True) if details else None,
        allowed_decisions=allowed,
    )


def _legacy_app_server_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a v2 app-server item to the existing audit-event vocabulary."""
    converted: dict[str, Any] = {}
    for key, value in item.items():
        converted[APP_SERVER_ITEM_FIELDS.get(key, key)] = value
    kind = str(item.get("type", "action"))
    converted["type"] = APP_SERVER_ITEM_TYPES.get(kind, kind)
    return converted


def messages_from_app_server_event(
    message: dict[str, Any], thread_id: str = "codex", event_index: int = 0
) -> tuple[Message, ...]:
    """Conversation messages completed by one app-server notification."""
    method = message.get("method")
    if method not in {"item/started", "item/completed"}:
        return ()
    params = message.get("params") or {}
    item = params.get("item") if isinstance(params, dict) else None
    if not isinstance(item, dict) or item.get("type") == "userMessage":
        return ()
    legacy = {
        "type": "item.started" if method == "item/started" else "item.completed",
        "item": _legacy_app_server_item(item),
    }
    return messages_from_event(legacy, thread_id, event_index)


def turn_from_app_server_events(events: Iterable[dict[str, Any]]) -> AgentTurn:
    """Assemble an ``AgentTurn`` from stable app-server v2 notifications."""
    legacy: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    terminal_status: str | None = None
    thread_id = "codex"

    for message in events:
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            continue
        if params.get("threadId"):
            thread_id = str(params["threadId"])
        if method == "item/completed" and isinstance(params.get("item"), dict):
            item = params["item"]
            if item.get("type") == "userMessage":
                continue
            legacy.append({"type": "item.completed", "item": _legacy_app_server_item(item)})
        elif method == "thread/tokenUsage/updated":
            last = (params.get("tokenUsage") or {}).get("last") or {}
            usage = {
                "input_tokens": int(last.get("inputTokens", 0)),
                "cached_input_tokens": int(last.get("cachedInputTokens", 0)),
                "output_tokens": int(last.get("outputTokens", 0)),
            }
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            terminal_status = str(turn.get("status", ""))

    if terminal_status in {"failed", "interrupted"}:
        legacy.append({"type": "turn.failed"})
    legacy.append({"type": "turn.completed", "usage": usage})
    return turn_from_events(
        ({"type": "thread.started", "thread_id": thread_id}, *legacy)
    )


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


def messages_from_event(
    event: dict[str, Any], thread_id: str = "codex", event_index: int = 0
) -> tuple[Message, ...]:
    """Conversation messages completed by one Codex event.

    This is the incremental counterpart to `turn_from_events`. Agent messages
    include both narration and the eventual answer; only the completed turn can
    distinguish the last one, but all of them are displayed the same way and
    remain in this order in its transcript.
    """
    event_type = event.get("type")
    if event_type not in {"item.started", "item.completed"}:
        return ()
    item = event.get("item") or {}
    kind = item.get("type")
    if kind in IGNORED_ITEM_TYPES:
        return ()
    if kind == "agent_message":
        if event_type == "item.completed" and item.get("text"):
            return (Message.assistant(str(item["text"])),)
        return ()
    call_id = f"{thread_id}:{item.get('id') or f'item-{event_index}'}"
    messages = action_messages(item, call_id)
    return messages[:1] if event_type == "item.started" else messages


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
            # A turn that failed before saying anything is all the user gets to
            # read, so it carries the reason rather than only the fact.
            reported = failure_message_of(events)
            return AgentTurn(
                message=Message.assistant(
                    f"Codex reported a failed turn: {reported}"
                    if reported
                    else "Codex reported a failed turn"
                ),
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


def failure_message_of(events: Iterable[dict[str, Any]]) -> str | None:
    """What Codex said went wrong, taken from the stream rather than stderr.

    A failing `codex exec` explains itself on *stdout*, as an `error` or
    `turn.failed` event, and leaves stderr to whatever it was going to warn
    about anyway -- a stale models cache, a deprecation. So the tail of stderr
    is reliably the wrong thing to report: a run that stopped because the
    account is out of quota says "failed to load models cache" there, and the
    sentence naming the quota and the date it resets is here.
    """
    for event in events:
        match event.get("type"):
            case "error":
                reported = event.get("message")
            case "turn.failed":
                error = event.get("error")
                reported = error.get("message") if isinstance(error, dict) else error
            case _:
                continue
        if reported:
            return str(reported)
    return None


def thread_id_of(events: Iterable[dict[str, Any]]) -> str | None:
    """Codex's own session id, if it announced one.

    Not used yet. It is what a future `codex exec resume` would need, and it is
    cheaper to read here than to re-derive later.
    """
    for event in events:
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


async def _write_json(
    stream: asyncio.StreamWriter, message: dict[str, Any]
) -> None:
    stream.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    await stream.drain()


async def _next_json_message(lines: AsyncIterator[bytes]) -> dict[str, Any]:
    async for line in lines:
        parsed = parse_events(line.decode(errors="replace"))
        if parsed:
            return parsed[0]
    raise CodexExecutionError("codex app-server closed its event stream unexpectedly")


async def _read_rpc_response(
    lines: AsyncIterator[bytes], request_id: int
) -> dict[str, Any]:
    """Read one startup response; startup emits no turn items to preserve."""
    while True:
        message = await _next_json_message(lines)
        if message.get("id") != request_id:
            continue
        if message.get("error"):
            error = message["error"]
            detail = error.get("message") if isinstance(error, dict) else error
            raise CodexExecutionError(f"codex app-server request failed: {detail}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexExecutionError("codex app-server returned a malformed response")
        return result


class CodexAgentRunner:
    """Runs an agent turn by shelling out to the Codex CLI.

    Implements `engine.ports.AgentRunner`, `StreamingAgentRunner`,
    `InteractiveAgentRunner` and `McpAgentRunner` -- one runner rather than one
    per transport, because which of Codex's two the CLI is driven over is a
    property of the turn being asked for and not of the agent answering it.

    `timeout_seconds=None` -- the default -- lets a turn take as long as it
    takes. A wall clock is the wrong thing to cut an agent off with: a long run
    is usually a large task, not a hung one, and killing it throws away every
    tool call it had already made. `cancel` is how a run ends early, because
    that is a decision with someone behind it. A deployment that wants a ceiling
    anyway can still pass one.
    """

    permission_translator = CODEX_PERMISSION_TRANSLATOR

    def __init__(
        self,
        binary_path: str = "codex",
        timeout_seconds: float | None = None,
        protocol_timeout_seconds: float = 60.0,
        sandbox: str = "read-only",
        working_directory: str = ".",
        model: str = "",
        workspace_provider: WorkspaceProvider | None = None,
    ) -> None:
        if sandbox not in SANDBOX_MODES:
            raise ValueError(f"sandbox must be one of {SANDBOX_MODES}, got {sandbox!r}")
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds
        self._protocol_timeout_seconds = protocol_timeout_seconds
        self._sandbox = sandbox
        self._working_directory = working_directory
        self._model = model
        self._workspace_provider = workspace_provider
        #: Live processes, so `cancel` has something to reach for.
        self._running: dict[AgentRunId, asyncio.subprocess.Process] = {}

    def command_line(
        self,
        profile: AgentProfile,
        working_directory: str | None = None,
        mcp_server: McpServerConfig | None = None,
    ) -> list[str]:
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
            working_directory or self._working_directory,
        ]
        if mcp_server is not None:
            prefix = f"mcp_servers.{mcp_server.name}"
            argv += [
                "-c",
                f"{prefix}.command={json.dumps(mcp_server.command)}",
                "-c",
                f"{prefix}.args={json.dumps(list(mcp_server.args))}",
            ]
        model = profile.model or self._model
        if model:
            argv += ["--model", model]
        return argv

    def app_server_command_line(self) -> list[str]:
        """The stable stdio app-server transport used for interactive turns."""
        return [self._binary_path, "app-server"]

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self.run_turn_streamed(
            agent_run_id,
            profile,
            messages,
            lambda _message: None,
            tools=tools,
            workspace_id=workspace_id,
        )

    async def run_turn_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_message: TurnObserver,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
        mcp_server: McpServerConfig | None = None,
    ) -> AgentTurn:
        """Run Codex, forwarding each completed JSONL item immediately."""
        if tools:
            raise CodexToolsUnsupportedError([tool.name for tool in tools])
        if workspace_id is not None and self._workspace_provider is None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        if shutil.which(self._binary_path) is None:
            raise CodexUnavailableError(
                f"{self._binary_path!r} is not on PATH -- install the Codex CLI, "
                "or point the runner at the binary"
            )

        working_directory = self._working_directory
        if workspace_id is not None:
            assert self._workspace_provider is not None
            working_directory = await self._workspace_provider.root_path(workspace_id)

        prompt = render_prompt(profile, messages)
        process = await asyncio.create_subprocess_exec(
            *self.command_line(profile, working_directory, mcp_server),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running[agent_run_id] = process
        try:
            events, stderr = await asyncio.wait_for(
                self._read_stream(process, prompt, on_message),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as timeout:
            process.kill()
            await process.wait()
            raise CodexExecutionError(
                f"Codex did not finish within {self._timeout_seconds:.0f}s"
            ) from timeout
        except asyncio.CancelledError:
            # The caller gave up -- do not leave the CLI running behind them.
            process.kill()
            await process.wait()
            raise
        except Exception:
            # Rendering callbacks are application code. If one fails, the turn
            # has no consumer and its subprocess must not be left behind.
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            self._running.pop(agent_run_id, None)

        if process.returncode != 0:
            # Codex's own explanation if it gave one, and the tail of stderr
            # only when it did not: reporting both would bury the sentence
            # somebody can act on under the warnings it prints regardless.
            raise CodexExecutionError(
                f"codex exited {process.returncode}: "
                f"{failure_message_of(events) or _tail(stderr)}"
            )
        turn = turn_from_events(events)
        return turn

    async def run_turn_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_approval: ApprovalHandler,
        on_message: TurnObserver | None = None,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Run a turn over app-server's bidirectional JSON-RPC transport."""
        if tools:
            raise CodexToolsUnsupportedError([tool.name for tool in tools])
        if workspace_id is not None and self._workspace_provider is None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        if shutil.which(self._binary_path) is None:
            raise CodexUnavailableError(
                f"{self._binary_path!r} is not on PATH -- install the Codex CLI, "
                "or point the runner at the binary"
            )

        working_directory = self._working_directory
        if workspace_id is not None:
            assert self._workspace_provider is not None
            working_directory = await self._workspace_provider.root_path(workspace_id)
        working_directory = os.path.abspath(working_directory)

        process = await asyncio.create_subprocess_exec(
            *self.app_server_command_line(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
        )
        self._running[agent_run_id] = process
        try:
            events, stderr = await asyncio.wait_for(
                self._read_app_server(
                    process,
                    render_prompt(profile, messages),
                    profile.model or self._model,
                    working_directory,
                    on_approval,
                    on_message or (lambda _message: None),
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as timeout:
            process.kill()
            await process.wait()
            raise CodexExecutionError(
                f"Codex did not finish within {self._timeout_seconds:.0f}s"
            ) from timeout
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            self._running.pop(agent_run_id, None)

        if process.returncode != 0:
            raise CodexExecutionError(
                f"codex app-server exited {process.returncode}: {_tail(stderr)}"
            )
        return turn_from_app_server_events(events)

    async def _read_app_server(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
        model: str,
        working_directory: str,
        on_approval: ApprovalHandler,
        on_message: TurnObserver,
    ) -> tuple[tuple[dict[str, Any], ...], str]:
        """Drive one app-server thread and turn over newline-delimited JSON-RPC."""
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(process.stderr.read())
        lines = read_lines(process.stdout).__aiter__()
        events: list[dict[str, Any]] = []
        observed: list[Message] = []
        started_actions: set[str] = set()
        try:
            await _write_json(
                process.stdin,
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "clientInfo": {
                            "name": "engine",
                            "title": "Engine",
                            "version": "0.0.0",
                        }
                    },
                },
            )
            try:
                await asyncio.wait_for(
                    _read_rpc_response(lines, 0),
                    timeout=self._protocol_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise CodexExecutionError(
                    "codex app-server did not complete initialization within "
                    f"{self._protocol_timeout_seconds:g}s"
                ) from error
            await _write_json(process.stdin, {"method": "initialized", "params": {}})

            thread_params: dict[str, Any] = {"cwd": working_directory}
            if model:
                thread_params["model"] = model
            await _write_json(
                process.stdin,
                {"method": "thread/start", "id": 1, "params": thread_params},
            )
            try:
                started = await asyncio.wait_for(
                    _read_rpc_response(lines, 1),
                    timeout=self._protocol_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise CodexExecutionError(
                    "codex app-server did not start a thread within "
                    f"{self._protocol_timeout_seconds:g}s"
                ) from error
            thread = started.get("thread") or {}
            thread_id = str(thread.get("id", ""))
            if not thread_id:
                raise CodexExecutionError("codex app-server did not return a thread id")

            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": working_directory,
                "approvalPolicy": INTERACTIVE_APPROVAL_POLICY,
                "sandboxPolicy": app_server_sandbox_policy(self._sandbox),
            }
            if model:
                turn_params["model"] = model
            await _write_json(
                process.stdin,
                {"method": "turn/start", "id": 2, "params": turn_params},
            )

            turn_response_seen = False
            while True:
                message = await _next_json_message(lines)
                if message.get("id") == 2 and "method" not in message:
                    if message.get("error"):
                        error = message["error"]
                        detail = error.get("message") if isinstance(error, dict) else error
                        raise CodexExecutionError(
                            f"codex app-server turn/start failed: {detail}"
                        )
                    turn_response_seen = True
                    continue

                request = approval_request_from_app_server(message)
                if request is not None:
                    decision = await on_approval(request)
                    if decision not in request.allowed_decisions:
                        raise CodexExecutionError(
                            f"approval decision {decision.value!r} is not allowed for "
                            f"{request.approval_id}"
                        )
                    await _write_json(
                        process.stdin,
                        {
                            "id": message["id"],
                            "result": {"decision": APP_SERVER_DECISIONS[decision]},
                        },
                    )
                    continue

                if "id" in message and message.get("method"):
                    method = str(message["method"])
                    await _write_json(
                        process.stdin,
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": f"unsupported server request {method}",
                            },
                        },
                    )
                    raise CodexExecutionError(
                        f"codex app-server requested unsupported interaction {method!r}"
                    )

                method = message.get("method")
                if method:
                    event_index = len(events)
                    events.append(message)
                    streamed = messages_from_app_server_event(
                        message, thread_id, event_index
                    )
                    params = message.get("params") or {}
                    item = params.get("item") if isinstance(params, dict) else None
                    item_id = str(item.get("id", "")) if isinstance(item, dict) else ""
                    if method == "item/started" and streamed and item_id:
                        started_actions.add(item_id)
                    elif (
                        method == "item/completed"
                        and item_id in started_actions
                        and len(streamed) == 2
                    ):
                        streamed = streamed[1:]
                    for completed in streamed:
                        observed.append(completed)
                        on_message(completed)
                    if method == "turn/completed":
                        break

            if not turn_response_seen:
                raise CodexExecutionError(
                    "codex app-server completed without acknowledging turn/start"
                )
            process.stdin.close()
            await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

        if process.returncode == 0:
            transcript = turn_from_app_server_events(events).transcript
            if transcript[: len(observed)] == tuple(observed):
                for completed in transcript[len(observed) :]:
                    on_message(completed)
        return tuple(events), stderr

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Run a turn with the runtime's bound terminal tools attached."""
        return await self.run_turn_streamed(
            agent_run_id,
            profile,
            messages,
            lambda _message: None,
            workspace_id=workspace_id,
            mcp_server=mcp_server,
        )

    async def _read_stream(
        self,
        process: asyncio.subprocess.Process,
        prompt: str,
        on_message: TurnObserver,
    ) -> tuple[tuple[dict[str, Any], ...], str]:
        """Feed stdin and drain both output pipes without buffering stdout."""
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        stderr_task = asyncio.create_task(process.stderr.read())
        events: list[dict[str, Any]] = []
        observed: list[Message] = []
        thread_id = "codex"
        started_actions: set[str] = set()
        try:
            async for line in read_lines(process.stdout):
                for event in parse_events(line.decode(errors="replace")):
                    event_index = len(events)
                    events.append(event)
                    if event.get("type") == "thread.started" and event.get("thread_id"):
                        thread_id = str(event["thread_id"])
                    streamed = messages_from_event(event, thread_id, event_index)
                    item = event.get("item") or {}
                    item_id = str(item.get("id", ""))
                    if event.get("type") == "item.started" and streamed and item_id:
                        started_actions.add(item_id)
                    elif (
                        event.get("type") == "item.completed"
                        and item_id in started_actions
                        and len(streamed) == 2
                    ):
                        streamed = streamed[1:]
                    for message in streamed:
                        observed.append(message)
                        on_message(message)
            await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

        # Failed turns and future event shapes may synthesize a terminal message
        # only once the whole turn is known. Forward just that unseen suffix.
        if process.returncode == 0:
            transcript = turn_from_events(events).transcript
            if transcript[: len(observed)] == tuple(observed):
                for message in transcript[len(observed) :]:
                    on_message(message)
        return tuple(events), stderr

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
    "APP_SERVER_APPROVAL_ENVELOPE",
    "CODEX_PERMISSION_TRANSLATOR",
    "INTERACTIVE_APPROVAL_POLICY",
    "SANDBOX_MODES",
    "CodexAgentRunner",
    "CodexExecutionError",
    "CodexPermissionTranslator",
    "CodexToolsUnsupportedError",
    "CodexUnavailableError",
    "action_messages",
    "app_server_sandbox_policy",
    "approval_request_from_app_server",
    "failure_message_of",
    "messages_from_app_server_event",
    "messages_from_event",
    "parse_events",
    "render_prompt",
    "thread_id_of",
    "turn_from_app_server_events",
    "turn_from_events",
]
