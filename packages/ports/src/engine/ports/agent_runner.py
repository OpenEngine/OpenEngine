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

Streaming is deliberately absent for now. It is a presentation concern, and
adding it later is an additional method rather than a change to this one.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from engine.domain.agents import AgentProfile
from engine.domain.chat import Message, ToolCall
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec


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
class TokenUsage:
    """What the turn cost, when the provider reports it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """One assistant response, plus whatever the provider said about it."""

    message: Message
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage | None = None

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """The tools the model asked for. Carried on the message, so that the
        request survives being appended to a conversation."""
        return self.message.tool_calls

    @property
    def wants_tools(self) -> bool:
        return bool(self.message.tool_calls)


@runtime_checkable
class AgentRunner(Protocol):
    """Runs one agent turn to completion."""

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


__all__ = ["AgentRunner", "AgentTurn", "FinishReason", "TokenUsage"]
