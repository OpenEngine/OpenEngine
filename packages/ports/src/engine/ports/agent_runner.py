"""Agent Runner capability.

Executes one turn of an agent: profile and history in, assistant message out.
Codex is one intended implementation; an OpenAI-compatible chat endpoint is
another. Both satisfy the same shape, which is the point -- talking to the
foreman and running a headless coder are the same call with different profiles.

The port is turn-shaped rather than task-shaped because tool use is a
conversation: the runner returns the tool calls the model asked for and stops.
Executing them, and deciding whether to go round again, belongs to the caller --
the runner never invokes a tool itself, so what an agent may do stays governed
by its profile rather than by whichever adapter happens to be running it.

Runners whose providers expose progress or pause for user approval may
additionally implement `StreamingAgentRunner`, `StreamingMcpAgentRunner`, or
`InteractiveAgentRunner`; workflow runners that need both MCP tools and
approvals implement `InteractiveMcpAgentRunner`. The original `run_turn` method
remains the fallback, so callers do not need to special-case runners with
neither capability.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from engine.domain.agents import AgentProfile
from engine.domain.approvals import ApprovalDecision, ApprovalKind
from engine.domain.chat import Message, ToolCall
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec
from engine.ports.permissions import PermissionTranslator


class FinishReason(Enum):
    """Why the model stopped."""

    STOP = "stop"
    """It finished its answer."""
    TOOL_CALLS = "tool_calls"
    """It wants tools run before it can continue."""
    LENGTH = "length"
    """It hit a token limit mid-answer."""
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A normalized request emitted while an agent turn is paused.

    What the *provider* asked, carrying the provider's own request id. What was
    made of it is `engine.domain.ApprovalRecord`, which is durable and keyed by
    an id of ours -- adapters never see that, and never need to.
    """

    approval_id: str
    kind: ApprovalKind
    reason: str | None = None
    command: str | None = None
    cwd: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    """The `ToolCall.call_id` this pause is about, when the provider says.

    The one field that ties a request to the thing it is a request *for*.
    Without it a reader is left inferring from order, which is wrong the moment
    a turn runs one command it may run unasked and another it may not. Optional
    because a provider may pause over something that is not a tool call at all.
    """
    arguments: str | None = None
    allowed_decisions: tuple[ApprovalDecision, ...] = tuple(ApprovalDecision)
    questions: tuple["UserInputQuestion", ...] = ()
    requires_human: bool = False
    """Whether configured and per-conversation auto-approval must be ignored."""


@dataclass(frozen=True, slots=True)
class UserInputOption:
    """One answer offered for a structured question."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class UserInputQuestion:
    """A provider-neutral question that can also accept a free-form answer."""

    question_id: str
    header: str
    question: str
    options: tuple[UserInputOption, ...] = ()
    multi_select: bool = False
    allows_other: bool = True


@dataclass(frozen=True, slots=True)
class UserInputAnswer:
    """The values a person selected or entered for one question."""

    question_id: str
    answers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UserInputResponse:
    """A complete structured response returned to a paused provider turn."""

    answers: tuple[UserInputAnswer, ...]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What the turn cost, when the provider reports it.

    `cached_prompt_tokens` is the part of `prompt_tokens` the provider served
    from its prompt cache. Reported because it is the only way to tell a prompt
    that is merely large from one that is large *and* being re-billed in full
    every turn.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_usd: float | None = None
    """What the provider says the turn cost, when it says. Few do."""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """One assistant response, plus whatever the provider said about it.

    `steps` and `tool_calls` both concern tools and mean opposite things. Keeping
    them apart is what lets one port serve two kinds of agent:

    * **`steps`** -- what the agent *already did* on the way to this answer:
      commands it ran, files it changed, narration along the way. Complete, in
      order, and requiring nothing of the caller but storage. An agent that runs
      its own tools (Codex) reports here.
    * **`message.tool_calls`** -- what the agent *wants done* before it can
      continue. The caller executes them, appends the results, and calls again.
      A chat-completions model reports here.

    Conflating them would be a live bug rather than an untidiness: `wants_tools`
    drives the caller's loop, so recording an already-executed command as a
    tool call would ask the caller to run it a second time.
    """

    message: Message
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage | None = None
    steps: tuple[Message, ...] = field(default=())

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """The tools the model asked for. Carried on the message, so that the
        request survives being appended to a conversation."""
        return self.message.tool_calls

    @property
    def wants_tools(self) -> bool:
        return bool(self.message.tool_calls)

    @property
    def transcript(self) -> tuple[Message, ...]:
        """Everything this turn adds to the conversation, in order.

        What a caller stores. The steps come first because they happened first.
        """
        return (*self.steps, self.message)


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """A provider-neutral stdio MCP server for one agent execution.

    The runtime creates the command and binds its opaque credentials to the
    workflow context. Adapters only translate this description into their
    provider CLI's configuration syntax.
    """

    name: str
    command: str
    args: tuple[str, ...] = field(default=())


@runtime_checkable
class AgentRunner(Protocol):
    """Runs one agent turn to completion."""

    permission_translator: PermissionTranslator
    """Maps this provider's approval requests into Engine permission scopes."""

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Answer as `profile`, given `messages` as the history so far.

        `messages` is the complete context to reason over; the runner does not
        load history of its own. `tools` is what the profile's grants resolved
        to -- a runner must not offer the model anything outside it.
        """
        ...

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        """Best-effort cancellation. Safe to call on an already-finished run."""
        ...


@runtime_checkable
class McpAgentRunner(AgentRunner, Protocol):
    """A CLI runner that can attach a runtime-provided stdio MCP server."""

    async def run_turn_with_mcp(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        ...


TurnObserver = Callable[[Message], None]
"""Receives each conversation message as soon as a runner completes it."""


@runtime_checkable
class StreamingMcpAgentRunner(McpAgentRunner, Protocol):
    """An MCP-capable runner that also exposes completed transcript messages."""

    async def run_turn_with_mcp_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        mcp_server: McpServerConfig,
        on_message: TurnObserver,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        ...


ApprovalResponse = ApprovalDecision | UserInputResponse
ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]
"""Presents a human interaction and waits for its decision or answers."""


@runtime_checkable
class StreamingAgentRunner(AgentRunner, Protocol):
    """An agent runner that can report a turn while it is still running."""

    async def run_turn_streamed(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        on_message: TurnObserver,
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        """Run a turn and synchronously observe its completed messages in order.

        The observed messages are exactly the turn transcript: narration, tool
        calls and their results, then the final assistant message. The returned
        turn remains authoritative for persistence and finish metadata.
        """
        ...


@runtime_checkable
class InteractiveAgentRunner(AgentRunner, Protocol):
    """An agent runner that may pause a turn for user approval."""

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
        """Run a turn, awaiting `on_approval` whenever user consent is needed."""
        ...


@runtime_checkable
class InteractiveMcpAgentRunner(McpAgentRunner, InteractiveAgentRunner, Protocol):
    """An MCP-capable runner that can also pause for user approval."""

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
        """Run with MCP tools, awaiting approval without losing those tools."""
        ...


__all__ = [
    "AgentRunner",
    "AgentTurn",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalKind",
    "ApprovalRequest",
    "ApprovalResponse",
    "FinishReason",
    "InteractiveAgentRunner",
    "InteractiveMcpAgentRunner",
    "McpAgentRunner",
    "McpServerConfig",
    "StreamingMcpAgentRunner",
    "PermissionTranslator",
    "StreamingAgentRunner",
    "TokenUsage",
    "TurnObserver",
    "UserInputAnswer",
    "UserInputOption",
    "UserInputQuestion",
    "UserInputResponse",
]
