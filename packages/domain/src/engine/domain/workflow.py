"""The step vocabulary a run-bound agent is held to.

What survives of the old workflow definition language: an agent invocation
declares the outputs it must produce, and reports them back. The graph runtime's
terminal MCP server is what asks for both today -- there is no compiled step
graph any more, only the contract between one node and the agent running in it.
"""

from dataclasses import dataclass, field

from engine.domain.ids import AgentId, StepId


@dataclass(frozen=True, slots=True)
class StepOutput:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class StepSpec:
    step_id: StepId
    agent_id: AgentId
    required_outputs: tuple[str, ...] = field(default=())


__all__ = [
    "StepOutput",
    "StepSpec",
]
