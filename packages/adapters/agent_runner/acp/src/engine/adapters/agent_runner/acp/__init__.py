"""Agent Runner capability, backed by an ACP agent.

``ACPAgentRunner`` runs a turn through a `langgraph-acp` provider --
`CodexACPProvider`, `ClaudeACPProvider`, or any `StdioACPProvider` -- rather
than driving a CLI's own protocol. The protocol is `langgraph-acp`'s; this
package binds it to the runner port:

    ACP                                     Engine
    session/update  agent_message_chunk     narration, then the answer
                    tool_call(_update)      a ToolCall, then its result
    session/request_permission              ApprovalRequest -> decision -> option
    elicitation/create                      ApprovalRequest.questions -> answers
    session/prompt  stopReason, usage       FinishReason, TokenUsage

Each turn is one agent process and one new ACP session, given the whole
conversation, for the same reason the CLI runners replay it: our conversation
is the source of truth, and a session the agent kept would be a second one.
That is also why "allow for this session" is answered with the agent's
allow-once option. The consent is Engine's session grant, which the broker
replays to the next process that asks; an agent-side "always" would outlive the
turn in the agent's own settings, where nobody reviews it.

Like the CLI runners, an ACP agent brings its own tools and cannot be handed
ours, so a profile with grants is refused -- except the runtime-bound MCP
server, which is attached to the session and whose tools are never re-asked
about: that server is Engine's own broker and decides for itself.

`codex_acp_runner` and `claude_acp_runner` translate Engine's settings into
what each adapter reads: a sandbox enforced under codex-acp for Codex (see
`codex_policy`), SDK options under `_meta.claudeCode.options` for Claude.
"""

import asyncio
import contextlib
import functools
import hashlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from langgraph_acp import (
    ACPAgentProvider,
    ACPClient,
    ACPElicitationRequest,
    ACPElicitationResponse,
    ACPEvent,
    ACPEventType,
    ACPPermissionOutcome,
    ACPPermissionRequest,
    ACPSession,
)
from langgraph_acp.providers import (
    CLAUDE_ACP_COMMAND,
    CODEX_ACP_COMMAND,
    ClaudeACPProvider,
    CodexACPProvider,
)

from engine.adapters.agent_runner.acp.claude import (
    READ_ONLY_TOOLS,
    allowed_tools_for,
    claude_session_config,
)
from engine.adapters.agent_runner.acp.codex_policy import (
    CODEX_PATH_VARIABLE,
    SANDBOX_POLICIES,
    SANDBOX_VARIABLE,
)
from engine.adapters.agent_runner.acp.permissions import (
    ACP_PERMISSION_TRANSLATOR,
    ACPPermissionTranslator,
)
from engine.adapters.agent_runner.acp.questions import (
    content_from_answers,
    questions_from_form,
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
    ResponseStyle,
    TokenUsage,
    TurnObserver,
    UserInputResponse,
)
from engine.ports.workspace_provider import WorkspaceProvider
from engine.runtime.session_grants import PATH_COLLECTION_FIELDS, PATH_FIELDS
from engine.runtime.transcript import flatten

#: The codex-acp preset every Codex session starts in: `on-request` approval,
#: with a person as the reviewer. The adapter's default, `agent`, has a model
#: approve requests on a person's behalf. The preset's sandbox is not used -- see
#: `engine.adapters.agent_runner.acp.codex_policy`, which replaces it, and the
#: preset's approval policy, on every turn.
CODEX_AGENT_MODE = "read-only"

#: The Codex sandbox names `codex_acp_runner` accepts.
CODEX_SANDBOXES = tuple(SANDBOX_POLICIES)

NO_ATTRIBUTION_INSTRUCTIONS = (
    "Do not add AI attribution to commits or pull requests, including "
    "Co-authored-by trailers or generated-by notices."
)

#: How ACP's stop reasons read as the port's. Anything unlisted is a stop.
FINISH_REASONS: Mapping[str, FinishReason] = {
    "end_turn": FinishReason.STOP,
    "cancelled": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "max_turn_requests": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
}

#: ACP tool kinds that change files, and the names a Codex call is recorded by.
FILE_KINDS = frozenset({"edit", "delete", "move"})
TOOL_NAMES: Mapping[str, str] = {
    "execute": "command_execution",
    "edit": "file_change",
    "delete": "file_change",
    "move": "file_change",
}
CLAUDE_FILE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

#: Where a tool's raw output keeps its text, in preference order.
OUTPUT_FIELDS = ("formatted_output", "aggregated_output", "output", "stdout", "result")


class ACPExecutionError(RuntimeError):
    """The agent ran and did not finish in time."""


class ACPToolsUnsupportedError(NotImplementedError):
    """A profile granted tools that an ACP agent cannot be offered."""

    def __init__(self, tool_names: Sequence[str]) -> None:
        super().__init__(
            f"ACP agents run their own tools and cannot be offered {list(tool_names)}; "
            "only the runtime-bound MCP server is available to them"
        )
        self.tool_names = tuple(tool_names)


def render_prompt(profile: AgentProfile, messages: Sequence[Message]) -> str:
    """The profile's instructions, then the conversation, as one prompt.

    Instructions first so the prompt stays append-only from turn to turn; see
    `engine.runtime.transcript`.
    """
    if not messages:
        raise ValueError("cannot run a turn with no messages")
    sections: list[str] = []
    if profile.instructions.strip():
        sections.append(f"# Your instructions\n\n{profile.instructions.strip()}")
    sections.append(flatten(messages))
    return "\n\n".join(sections)


@dataclass(slots=True)
class _Running:
    """What `cancel` needs to reach a turn in flight."""

    client: ACPClient
    session: ACPSession | None = None


class ACPAgentRunner:
    """Runs an agent turn in a fresh ACP session.

    Implements `engine.ports.AgentRunner`, `StreamingAgentRunner`,
    `InteractiveAgentRunner`, `McpAgentRunner`, `StreamingMcpAgentRunner`, and
    `InteractiveMcpAgentRunner`. A turn run without an approval handler has
    nobody to ask, so every permission request is refused.

    `provider` is a dataclass provider -- the handler answering permission
    requests is the turn's, so each turn connects through a copy of it.
    `session_config` is sent with `session/new`; the effective model is added to
    it, and `langgraph-acp` applies that as the session's `model` option.
    `timeout_seconds=None` lets a turn take as long as it takes, as the CLI
    runners do: `cancel` is how one ends early.
    """

    permission_translator: ACPPermissionTranslator = ACP_PERMISSION_TRANSLATOR

    def __init__(
        self,
        provider: ACPAgentProvider,
        *,
        working_directory: str = ".",
        model: str = "",
        timeout_seconds: float | None = None,
        workspace_provider: WorkspaceProvider | None = None,
        session_config: Mapping[str, Any] | None = None,
    ) -> None:
        self._provider = provider
        self._working_directory = working_directory
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._workspace_provider = workspace_provider
        self._session_config = dict(session_config or {})
        self._running: dict[AgentRunId, _Running] = {}

    @property
    def provider(self) -> ACPAgentProvider:
        return self._provider

    def session_config_for(self, profile: AgentProfile) -> dict[str, Any] | None:
        """What `session/new` is sent for this profile. Public for inspection."""
        config = dict(self._session_config)
        model = profile.model or self._model
        if model:
            config["model"] = model
        return config or None

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self._turn(
            agent_run_id, profile, messages, tools=tools, workspace_id=workspace_id
        )

    async def run_turn_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_message: TurnObserver,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self._turn(
            agent_run_id,
            profile,
            messages,
            tools=tools,
            workspace_id=workspace_id,
            on_message=on_message,
        )

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
        return await self._turn(
            agent_run_id,
            profile,
            messages,
            tools=tools,
            workspace_id=workspace_id,
            on_message=on_message,
            on_approval=on_approval,
        )

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self._turn(
            agent_run_id,
            profile,
            messages,
            workspace_id=workspace_id,
            mcp_server=mcp_server,
        )

    async def run_turn_with_mcp_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        on_message: TurnObserver,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self._turn(
            agent_run_id,
            profile,
            messages,
            workspace_id=workspace_id,
            mcp_server=mcp_server,
            on_message=on_message,
        )

    async def run_turn_with_mcp_interactive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        on_approval: ApprovalHandler,
        on_message: TurnObserver | None = None,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        return await self._turn(
            agent_run_id,
            profile,
            messages,
            workspace_id=workspace_id,
            mcp_server=mcp_server,
            on_message=on_message,
            on_approval=on_approval,
        )

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        """Ask the agent to stop, and end its process if it does not.

        `session/cancel` first, so a turn that stops cleanly returns what it
        did; the connection is closed only if it has not within a second.
        """
        running = self._running.get(agent_run_id)
        if running is None:
            return
        if running.session is not None:
            with contextlib.suppress(Exception):
                await running.session.cancel()
            for _ in range(20):
                if agent_run_id not in self._running:
                    return
                await asyncio.sleep(0.05)
        await running.client.close()

    async def _turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
        mcp_server: McpServerConfig | None = None,
        on_message: TurnObserver | None = None,
        on_approval: ApprovalHandler | None = None,
    ) -> AgentTurn:
        if tools:
            raise ACPToolsUnsupportedError([tool.name for tool in tools])
        prompt = render_prompt(profile, messages)
        working_directory = await self._working_directory_for(workspace_id)
        try:
            return await asyncio.wait_for(
                self._drive(
                    agent_run_id,
                    profile,
                    prompt,
                    working_directory,
                    mcp_server,
                    on_message or (lambda _message: None),
                    on_approval,
                ),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as timeout:
            raise ACPExecutionError(
                f"{self._provider.name} did not finish within "
                f"{self._timeout_seconds:.0f}s"
            ) from timeout

    async def _working_directory_for(self, workspace_id: WorkspaceId | None) -> str:
        if workspace_id is None:
            return os.path.abspath(self._working_directory)
        if self._workspace_provider is None:
            raise NotImplementedError(
                "resolving a WorkspaceId to a path needs the workspace provider; "
                "until then this runner works in its configured directory"
            )
        return os.path.abspath(await self._workspace_provider.root_path(workspace_id))

    async def _drive(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        prompt: str,
        working_directory: str,
        mcp_server: McpServerConfig | None,
        on_message: TurnObserver,
        on_approval: ApprovalHandler | None,
    ) -> AgentTurn:
        turn = _Turn(
            agent=self._provider.name,
            agent_run_id=agent_run_id,
            working_directory=working_directory,
            on_message=on_message,
            on_approval=on_approval,
            mcp_server=mcp_server.name if mcp_server is not None else None,
        )
        # The provider's own permission handler is a policy fixed at
        # construction; here it is this turn, so the turn gets a copy. Questions
        # are offered only to a turn with someone to put them to: an agent told
        # the client cannot answer keeps them to itself.
        handlers: dict[str, Any] = {"permissions": turn.answer}
        if on_approval is not None and "elicitations" in {
            field.name for field in fields(self._provider)  # type: ignore[arg-type]
        }:
            handlers["elicitations"] = turn.elicit
        client = await replace(self._provider, **handlers).connect()  # type: ignore[type-var]
        running = _Running(client)
        self._running[agent_run_id] = running
        try:
            session = await client.new_session(
                cwd=working_directory,
                mcp_servers=[_acp_mcp_server(mcp_server)] if mcp_server else [],
                session_config=self.session_config_for(profile),
            )
            running.session = session
            async with contextlib.aclosing(session.prompt(prompt)) as events:
                async for event in events:
                    await turn.observe(event)
        finally:
            await turn.close()
            self._running.pop(agent_run_id, None)
            await client.close()
        return turn.result()


def _acp_mcp_server(server: McpServerConfig) -> dict[str, Any]:
    """An ACP stdio `mcpServers` entry. `env` is required even when empty."""
    return {
        "name": server.name,
        "command": server.command,
        "args": list(server.args),
        "env": [],
    }


class _Turn:
    """One turn's messages and permission requests, as they arrive.

    Two tasks feed it. The turn's own loop observes the event stream, and the
    connection answers `session/request_permission` on a task of its own. The
    permission request is streamed before it is answered, so the answer waits
    until the loop has observed it: everything the agent said and did before
    asking is then published before the question is put to anyone.
    """

    def __init__(
        self,
        *,
        agent: str,
        agent_run_id: AgentRunId,
        working_directory: str,
        on_message: TurnObserver,
        on_approval: ApprovalHandler | None,
        mcp_server: str | None,
    ) -> None:
        self._agent = agent
        self._run = agent_run_id
        self._working_directory = working_directory
        self._on_message = on_message
        self._on_approval = on_approval
        self._mcp_server = mcp_server
        self._entries: list[Message] = []
        self._text: list[str] = []
        self._calls: dict[str, dict[str, Any]] = {}
        self._announced: set[str] = set()
        self._finished: set[str] = set()
        self._stop_reason: str | None = None
        self._usage: Mapping[str, Any] | None = None
        self._cost: float | None = None
        self._asked = 0
        self._observed = 0
        self._closed = False
        self._progress = asyncio.Condition()

    # --- the event stream ---------------------------------------------------

    async def observe(self, event: ACPEvent) -> None:
        data = event.data
        match event.type:
            case ACPEventType.MESSAGE_DELTA:
                content = data.get("content")
                if isinstance(content, Mapping) and content.get("type") == "text":
                    self._text.append(str(content.get("text", "")))
            case ACPEventType.TOOL_STARTED | ACPEventType.TOOL_UPDATED:
                self._flush()
                self._tool(data)
            case ACPEventType.PERMISSION_REQUESTED | ACPEventType.ELICITATION_REQUESTED:
                self._flush()
                tool_call = data.get("toolCall")
                if isinstance(tool_call, Mapping) and tool_call.get("toolCallId"):
                    self._merge(tool_call)
                    self._announce(str(tool_call["toolCallId"]))
                async with self._progress:
                    self._observed += 1
                    self._progress.notify_all()
            case ACPEventType.USAGE_UPDATED:
                cost = data.get("cost")
                if isinstance(cost, Mapping) and cost.get("currency") in (None, "USD"):
                    amount = cost.get("amount")
                    if isinstance(amount, (int, float)):
                        self._cost = float(amount)
            case ACPEventType.PROMPT_COMPLETED:
                stop = data.get("stopReason")
                self._stop_reason = stop if isinstance(stop, str) else None
                usage = data.get("usage")
                self._usage = usage if isinstance(usage, Mapping) else None

    async def close(self) -> None:
        """Release any permission request still waiting on the stream."""
        async with self._progress:
            self._closed = True
            self._progress.notify_all()

    def result(self) -> AgentTurn:
        self._flush()
        spoken = [
            index
            for index, message in enumerate(self._entries)
            if message.content and not message.tool_calls and message.tool_call_id is None
        ]
        finish = FINISH_REASONS.get(self._stop_reason or "", FinishReason.STOP)
        if spoken:
            answer_at = spoken[-1]
            message = self._entries[answer_at]
            steps = self._entries[:answer_at] + self._entries[answer_at + 1 :]
        else:
            message = Message.assistant(
                f"{self._agent} ended its turn without a reply "
                f"({self._stop_reason or 'no stop reason'})."
            )
            self._emit(message)
            steps = self._entries[:-1]
        return AgentTurn(
            message=message,
            finish_reason=finish,
            usage=self._token_usage(),
            steps=tuple(steps),
        )

    def _emit(self, message: Message) -> None:
        self._entries.append(message)
        self._on_message(message)

    def _flush(self) -> None:
        """Publish the words gathered so far, as one message."""
        text = "".join(self._text)
        self._text.clear()
        if text.strip():
            self._emit(Message.assistant(text))

    def _merge(self, update: Mapping[str, Any]) -> dict[str, Any]:
        call = self._calls.setdefault(str(update["toolCallId"]), {})
        for key, value in update.items():
            if value not in (None, "", [], {}):
                call[key] = value
        return call

    def _tool(self, update: Mapping[str, Any]) -> None:
        if not update.get("toolCallId"):
            return
        call_id = str(update["toolCallId"])
        call = self._merge(update)
        status = call.get("status")
        if call.get("rawInput") or status in ("completed", "failed"):
            self._announce(call_id)
        if status in ("completed", "failed") and call_id not in self._finished:
            self._finished.add(call_id)
            output = _output_of(call)
            if status == "failed":
                output = f"error: {output}" if output else "error"
            self._emit(Message.tool_result(self._call_id(call_id), output))

    def _announce(self, call_id: str) -> None:
        """Record a call once, when enough of it is known to be worth reading."""
        if call_id in self._announced:
            return
        self._announced.add(call_id)
        call = self._calls.get(call_id, {})
        self._emit(
            Message.assistant(
                tool_calls=(
                    ToolCall(
                        call_id=self._call_id(call_id),
                        name=_tool_name(call),
                        arguments=json.dumps(_raw_input(call), sort_keys=True),
                    ),
                )
            )
        )

    def _call_id(self, call_id: str) -> str:
        # Agents number their calls per session, and every turn is a new one.
        return f"{self._run}:{call_id}"

    def _token_usage(self) -> TokenUsage | None:
        if self._usage is None and self._cost is None:
            return None
        usage = self._usage or {}
        fresh = _count(usage, "inputTokens")
        read = _count(usage, "cachedReadTokens")
        written = _count(usage, "cachedWriteTokens")
        return TokenUsage(
            prompt_tokens=fresh + read + written,
            completion_tokens=_count(usage, "outputTokens"),
            cached_prompt_tokens=read,
            cost_usd=self._cost,
        )

    # --- session/request_permission and elicitation/create ------------------

    async def _heard(self) -> int | None:
        """Wait until the stream has shown this request; `None` if it never will.

        Both kinds of request are streamed before their handler is called, with
        nothing awaited in between, so the nth request asked is the nth observed.
        """
        self._asked += 1
        asked = self._asked
        async with self._progress:
            await self._progress.wait_for(
                lambda: self._observed >= asked or self._closed
            )
        return None if self._closed else asked

    async def answer(self, request: ACPPermissionRequest) -> ACPPermissionOutcome:
        asked = await self._heard()
        if asked is None:
            return ACPPermissionOutcome.cancelled()
        call = self._merged(request)
        mcp_tool = _mcp_tool(call) or ""
        if self._mcp_server and mcp_tool.startswith(f"mcp__{self._mcp_server}__"):
            return _allowing(request)
        if self._on_approval is None:
            return _refusing(request)
        approval = self._approval_request(request, call, asked)
        decision = await self._on_approval(approval)
        if isinstance(decision, ApprovalDecision) and (
            decision is ApprovalDecision.CANCEL
            or decision not in approval.allowed_decisions
        ):
            return _refusing(request)
        return _allowing(request)

    async def elicit(self, request: ACPElicitationRequest) -> ACPElicitationResponse:
        """A question for the user: Claude's AskUserQuestion, Codex's user input.

        Answered with the user's answers, or cancelled -- which abandons the
        tool call that asked -- when they decline to give any.
        """
        asked = await self._heard()
        if asked is None or self._on_approval is None:
            return ACPElicitationResponse.cancel()
        questions = questions_from_form(request.message, request.requested_schema)
        call = self._calls.get(request.tool_call_id or "", {})
        tool_name = _claude_tool(call)
        approval = ApprovalRequest(
            approval_id=f"{self._run}:elicitation-{asked}",
            kind=ApprovalKind.USER_INPUT,
            reason=request.message or None,
            cwd=self._working_directory,
            tool_name=tool_name,
            tool_call_id=(
                self._call_id(request.tool_call_id) if request.tool_call_id else None
            ),
            allowed_decisions=(
                (ApprovalDecision.CANCEL,)
                if questions
                else (ApprovalDecision.ACCEPT, ApprovalDecision.CANCEL)
            ),
            questions=questions,
            requires_human=True,
        )
        decision = await self._on_approval(approval)
        if isinstance(decision, UserInputResponse):
            return ACPElicitationResponse.accept(
                content_from_answers(request.requested_schema, decision)
            )
        if decision is ApprovalDecision.ACCEPT and not questions:
            return ACPElicitationResponse.accept({})
        return ACPElicitationResponse.cancel()

    def _merged(self, request: ACPPermissionRequest) -> dict[str, Any]:
        """The request's tool call, with what the stream already said about it."""
        call = dict(request.tool_call)
        call_id = call.get("toolCallId")
        if isinstance(call_id, str) and call_id in self._calls:
            call = {**self._calls[call_id], **call}
        return call

    def _approval_request(
        self, request: ACPPermissionRequest, call: Mapping[str, Any], asked: int
    ) -> ApprovalRequest:
        call_id = call.get("toolCallId")
        arguments = _raw_input(call)
        mcp_tool = _mcp_tool(call)
        tool_name = mcp_tool or _claude_tool(call) or str(call.get("kind") or "") or None
        kind = (
            ApprovalKind.TOOL_USE
            if mcp_tool is not None
            else _approval_kind(tool_name, call.get("kind"))
        )
        requires_human = kind in (ApprovalKind.PLAN_APPROVAL, ApprovalKind.USER_INPUT)
        command = arguments.get("command")
        if isinstance(command, list):
            command = shlex.join(str(part) for part in command)
        cwd = arguments.get("cwd")
        if kind is ApprovalKind.FILE_CHANGE and not any(
            arguments.get(field) for field in (*PATH_FIELDS, *PATH_COLLECTION_FIELDS)
        ):
            # What a grant for a file change is scoped by: the paths it touches.
            paths = [
                str(location["path"])
                for location in call.get("locations") or ()
                if isinstance(location, Mapping) and location.get("path")
            ]
            if paths:
                arguments["paths"] = paths
        allows = any(option.kind.startswith("allow") for option in request.options)
        allowed: tuple[ApprovalDecision, ...] = (
            (
                ApprovalDecision.ACCEPT,
                *(() if requires_human else (ApprovalDecision.ACCEPT_FOR_SESSION,)),
                ApprovalDecision.CANCEL,
            )
            if allows
            else (ApprovalDecision.CANCEL,)
        )
        title = call.get("title")
        return ApprovalRequest(
            approval_id=f"{self._run}:{call_id or f'permission-{asked}'}",
            kind=kind,
            reason=str(title) if title else None,
            command=str(command) if command else None,
            cwd=str(cwd) if isinstance(cwd, str) and cwd else self._working_directory,
            tool_name=tool_name,
            tool_call_id=self._call_id(call_id) if isinstance(call_id, str) else None,
            arguments=json.dumps(arguments, sort_keys=True) if arguments else None,
            allowed_decisions=allowed,
            requires_human=requires_human,
        )


def _allowing(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """The agent's allow-once option; any allowing one if it offers no such thing.

    Never an "always" when "once" is on offer: consent that outlives the turn
    is Engine's session grant, not the agent's settings file.
    """
    for allows in (lambda kind: kind == "allow_once", lambda kind: kind.startswith("allow")):
        for option in request.options:
            if allows(option.kind):
                return ACPPermissionOutcome.selected(option.option_id)
    return ACPPermissionOutcome.cancelled()


def _refusing(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """The agent's reject-once option, so it can say it stopped; else cancel."""
    for option in request.options:
        if option.kind == "reject_once":
            return ACPPermissionOutcome.selected(option.option_id)
    return ACPPermissionOutcome.cancelled()


def _mcp_tool(call: Mapping[str, Any]) -> str | None:
    """`mcp__server__tool` for a call to an MCP tool, else Claude's tool name.

    Claude names MCP tools that way itself. Codex reports them as `execute`
    with the server and tool in `rawInput`, and is given the same spelling so
    a policy -- and the bound-server check -- reads both alike.
    """
    claude = _claude_tool(call)
    if claude is not None:
        return claude if claude.startswith("mcp__") else None
    raw = call.get("rawInput")
    if isinstance(raw, Mapping):
        server, tool = raw.get("server"), raw.get("tool")
        if isinstance(server, str) and isinstance(tool, str) and server and tool:
            return f"mcp__{server}__{tool}"
    return None


def _claude_tool(call: Mapping[str, Any]) -> str | None:
    meta = call.get("_meta")
    claude = meta.get("claudeCode") if isinstance(meta, Mapping) else None
    name = claude.get("toolName") if isinstance(claude, Mapping) else None
    return name if isinstance(name, str) and name else None


def _approval_kind(tool_name: str | None, kind: object) -> ApprovalKind:
    match tool_name:
        case "AskUserQuestion":
            return ApprovalKind.USER_INPUT
        case "ExitPlanMode" | "switch_mode":
            return ApprovalKind.PLAN_APPROVAL
        case "Bash":
            return ApprovalKind.COMMAND_EXECUTION
    if tool_name in CLAUDE_FILE_TOOLS or kind in FILE_KINDS:
        return ApprovalKind.FILE_CHANGE
    if kind == "execute":
        return ApprovalKind.COMMAND_EXECUTION
    return ApprovalKind.TOOL_USE


def _raw_input(call: Mapping[str, Any]) -> dict[str, Any]:
    raw = call.get("rawInput")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {} if raw is None else {"input": raw}


def _tool_name(call: Mapping[str, Any]) -> str:
    kind = call.get("kind")
    return (
        _claude_tool(call)
        or TOOL_NAMES.get(str(kind))
        or str(call.get("title") or kind or "tool")
    )


def _output_of(call: Mapping[str, Any]) -> str:
    """A finished call's result: its text content, else its raw output."""
    parts: list[str] = []
    for block in call.get("content") or ():
        if not isinstance(block, Mapping) or block.get("type") != "content":
            continue
        inner = block.get("content")
        if isinstance(inner, Mapping) and inner.get("type") == "text":
            parts.append(str(inner.get("text", "")))
    output = "\n".join(part for part in parts if part)
    raw = call.get("rawOutput")
    if not output and isinstance(raw, str):
        output = raw
    elif not output and isinstance(raw, Mapping):
        output = next(
            (str(raw[key]) for key in OUTPUT_FIELDS if raw.get(key) not in (None, "")),
            "",
        )
    exit_code = raw.get("exit_code") if isinstance(raw, Mapping) else None
    if exit_code is not None:
        output = f"{output}\n(exit {exit_code})".strip()
    return output


def _count(usage: Mapping[str, Any], name: str) -> int:
    value = usage.get(name)
    return int(value) if isinstance(value, (int, float)) else 0


# --- the two agents Engine ships with --------------------------------------


def codex_acp_runner(
    *,
    command: Sequence[str] = CODEX_ACP_COMMAND,
    sandbox: str = "workspace-write",
    working_directory: str = ".",
    model: str = "",
    timeout_seconds: float | None = None,
    workspace_provider: WorkspaceProvider | None = None,
    attribution: bool = True,
    env: Mapping[str, str] | None = None,
) -> ACPAgentRunner:
    """Codex, through codex-acp, in the sandbox Engine names.

    codex-acp cannot be asked for a sandbox, so it is handed a Codex that
    enforces one: `CODEX_PATH` names `codex_policy`, which pins every turn to
    `sandbox` with `on-request` approval. The operator's own `CODEX_PATH`, if
    any, is the Codex that runs underneath. Attribution reaches Codex as
    `developer_instructions` in `CODEX_CONFIG`, which codex-acp reads from its
    environment rather than from the protocol.
    """
    if sandbox not in CODEX_SANDBOXES:
        raise ValueError(f"sandbox must be one of {CODEX_SANDBOXES}, got {sandbox!r}")
    codex_env = {
        **(env or {}),
        "INITIAL_AGENT_MODE": CODEX_AGENT_MODE,
        "CODEX_PATH": _codex_policy_launcher(),
        SANDBOX_VARIABLE: sandbox,
    }
    codex_path = (env or {}).get("CODEX_PATH") or os.environ.get("CODEX_PATH")
    if codex_path:
        codex_env[CODEX_PATH_VARIABLE] = codex_path
    if not attribution:
        codex_env["CODEX_CONFIG"] = json.dumps(
            {"developer_instructions": NO_ATTRIBUTION_INSTRUCTIONS}
        )
    return ACPAgentRunner(
        CodexACPProvider(command=command, env=codex_env),
        working_directory=working_directory,
        model=model,
        timeout_seconds=timeout_seconds,
        workspace_provider=workspace_provider,
    )


@functools.cache
def _codex_policy_launcher() -> str:
    """An executable that runs `codex_policy` under this interpreter.

    `CODEX_PATH` is started as a program with `app-server` as its only argument,
    so it has to be a file. Written once per interpreter, and atomically, since
    two processes may race to write the same one.
    """
    interpreter = sys.executable
    module = "engine.adapters.agent_runner.acp.codex_policy"
    if os.name == "nt":
        suffix, body = ".cmd", f'@"{interpreter}" -m {module} %*\r\n'
    else:
        suffix, body = "", f'#!/bin/sh\nexec {shlex.quote(interpreter)} -m {module} "$@"\n'
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / "engine-codex-policy"
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / f"codex-{digest}{suffix}"
    if not launcher.is_file():
        staged = directory / f".{launcher.name}.{os.getpid()}"
        staged.write_text(body, encoding="utf-8")
        staged.chmod(0o755)
        os.replace(staged, launcher)
    return str(launcher)


def claude_acp_runner(
    *,
    command: Sequence[str] = CLAUDE_ACP_COMMAND,
    allowed_tools: Sequence[str] = READ_ONLY_TOOLS,
    tools: Sequence[str] | None = None,
    working_directory: str = ".",
    model: str = "",
    timeout_seconds: float | None = None,
    workspace_provider: WorkspaceProvider | None = None,
    attribution: bool = True,
    output_style: ResponseStyle | None = None,
    env: Mapping[str, str] | None = None,
) -> ACPAgentRunner:
    """Claude Code, through claude-agent-acp, with Engine's tool settings.

    `allowed_tools` run without asking; anything else reaches the permission
    handler. `tools` limits which of Claude's built-in tools exist at all --
    `READ_ONLY_TOOLS` for an agent that only reads -- and `None` keeps them all.
    Both, like attribution and output style, are SDK options the adapter reads
    from `_meta.claudeCode.options`.

    The session is pinned to Claude's `default` permission mode. The adapter
    otherwise starts in whatever mode the operator's own Claude settings name,
    and one like `auto` approves requests without them ever reaching Engine.
    """
    base = claude_session_config(attribution=attribution, output_style=output_style)
    options: dict[str, Any] = dict((base or {}).get("claudeCode", {}).get("options", {}))
    options["allowedTools"] = list(allowed_tools)
    if tools is not None:
        options["tools"] = list(tools)
    return ACPAgentRunner(
        ClaudeACPProvider(command=command, env=env),
        working_directory=working_directory,
        model=model,
        timeout_seconds=timeout_seconds,
        workspace_provider=workspace_provider,
        session_config={"mode": "default", "claudeCode": {"options": options}},
    )


__all__ = [
    "ACP_PERMISSION_TRANSLATOR",
    "CODEX_AGENT_MODE",
    "CODEX_SANDBOXES",
    "READ_ONLY_TOOLS",
    "NO_ATTRIBUTION_INSTRUCTIONS",
    "ACPAgentRunner",
    "ACPExecutionError",
    "ACPPermissionTranslator",
    "ACPToolsUnsupportedError",
    "allowed_tools_for",
    "claude_acp_runner",
    "claude_session_config",
    "codex_acp_runner",
    "render_prompt",
]
