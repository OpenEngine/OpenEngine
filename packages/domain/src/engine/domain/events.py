"""Events: things that have already happened.

Events are facts, stated in the past tense -- an adapter emits one only after
the world has actually changed. Compare `commands`, which are requests for
change.

What is left is the pair a run-bound agent reports through its terminal MCP
tools: it finished the node it was given, or it could not.
"""

from dataclasses import dataclass, field

from engine.domain.ids import AgentRunId, RunId, StepId
from engine.domain.workflow import StepOutput


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for every engine input."""

    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepCompleted(Event):
    """An agent finished its node with an outcome and its declared outputs."""

    step_id: StepId
    agent_run_id: AgentRunId
    outcome: str
    summary: str
    outputs: tuple[StepOutput, ...] = field(default=())
    mcp_request_id: str | int | None = None
    """JSON-RPC request that submitted the result, absent for non-MCP producers."""


@dataclass(frozen=True, slots=True)
class RunFailed(Event):
    """An unrecoverable failure ended the run."""

    reason: str
    agent_run_id: AgentRunId | None = None
    """The bound agent execution that reported the failure, when applicable."""
    mcp_request_id: str | int | None = None
    """JSON-RPC request that submitted the failure, absent for other failures."""


__all__ = [
    "Event",
    "RunFailed",
    "StepCompleted",
]
