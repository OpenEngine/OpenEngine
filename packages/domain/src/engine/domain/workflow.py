"""Workflow vocabulary."""

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


__all__ = ["StepOutput", "StepSpec"]
