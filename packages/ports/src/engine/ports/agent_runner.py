"""Agent Runner capability.

Runs a coding agent. Anthropic, OpenAI, and Strands are all intended to satisfy this
port, so nothing here names a vendor and nothing assumes a particular loop.

The port is deliberately *tool-aware*. A planner and a worker are the same kind
of thing running through this same interface -- the only difference between them
is which tools they are handed. That is the whole architectural claim of the
planner work, so it lives in the type signature rather than in a comment:

    planner = runner.start(AgentSpec(tools=PLANNING_TOOLS + [dispatch_task]), ...)
    worker  = runner.start(AgentSpec(tools=WORKER_TOOLS), ...)

Tools are JSON Schema, the lowest common denominator every agent backend speaks.
The runner never executes a tool itself -- it calls back into the `ToolInvoker`
the host supplied, so the host keeps the approval gate, the audit log, and the
ability to turn a tool call into an engine event.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from engine.domain.ids import AgentId, AttemptId, WorkspaceId


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool offered to an agent.

    `input_schema` is a JSON Schema object. Keep descriptions prescriptive about
    *when* to call the tool, not just what it does -- that is what actually moves
    a model's tool-selection behaviour.
    """

    name: str
    description: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the host got back from executing a tool call."""

    content: str
    is_error: bool = False


#: Executes one tool call on the host's behalf. The runner awaits this and feeds
#: the result back to the model; it never runs a tool itself.
ToolInvoker = Callable[[str, Mapping[str, object]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Everything needed to stand up one agent."""

    agent_id: AgentId
    system_prompt: str
    tools: tuple[ToolSpec, ...] = field(default=())
    workspace_id: WorkspaceId | None = None
    #: Backend-specific model hint. An adapter that has only one model ignores it.
    model: str | None = None


# --- events observed while an agent works ----------------------------------


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Base class for everything a session reports."""


@dataclass(frozen=True, slots=True)
class TextDelta(AgentEvent):
    """A fragment of the agent's user-facing text, as it is produced."""

    text: str


@dataclass(frozen=True, slots=True)
class Thinking(AgentEvent):
    """The agent is reasoning. Carries a summary if the backend exposes one."""

    summary: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallStarted(AgentEvent):
    """The agent asked for a tool. The host is about to run it."""

    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolCallFinished(AgentEvent):
    """The host ran the tool and handed the result back."""

    call_id: str
    name: str
    result: ToolResult


@dataclass(frozen=True, slots=True)
class TurnFinished(AgentEvent):
    """The agent stopped. `stop_reason` is backend-specific and informational."""

    stop_reason: str = "end_turn"


@runtime_checkable
class AgentSession(Protocol):
    """One live agent. Stateful: it remembers the conversation across sends."""

    def send(self, message: str) -> AsyncIterator[AgentEvent]:
        """Deliver a message and stream what the agent does in response.

        Not `async def` -- calling it returns an async iterator, so a caller
        writes `async for event in session.send(text)`.
        """
        ...

    async def close(self) -> None:
        """Release backend resources. Idempotent."""
        ...


@runtime_checkable
class AgentRunner(Protocol):
    """Starts agents. The one capability a planner and a worker share."""

    def start(self, spec: AgentSpec, invoke_tool: ToolInvoker) -> AgentSession:
        ...


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Outcome of running an agent to completion on a single task."""

    attempt_id: AttemptId
    succeeded: bool
    summary: str
    changed_files: tuple[str, ...] = field(default=())


def tool_names(tools: Sequence[ToolSpec]) -> tuple[str, ...]:
    return tuple(tool.name for tool in tools)


__all__ = [
    "AgentEvent",
    "AgentRunner",
    "AgentSession",
    "AgentSpec",
    "AttemptResult",
    "TextDelta",
    "Thinking",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolInvoker",
    "ToolResult",
    "ToolSpec",
    "TurnFinished",
    "tool_names",
]
