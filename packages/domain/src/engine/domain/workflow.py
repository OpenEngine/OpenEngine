"""Immutable workflow vocabulary compiled by the public Python DSL."""

from dataclasses import dataclass, field
from enum import Enum

from engine.domain.agents import AgentProfile
from engine.domain.ids import AgentId, StepId, WorkflowId


@dataclass(frozen=True, slots=True)
class StepOutput:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class StepSpec:
    step_id: StepId
    agent_id: AgentId
    required_outputs: tuple[str, ...] = field(default=())
    editable: bool = False
    """Whether a person may interrupt and continue this step's conversation."""


class WorkspaceAccess(Enum):
    """The workspace authority an agent step receives from the runtime."""

    READ = "read"
    WRITE = "write"


class TerminalOutcome(Enum):
    """A transition that ends the whole workflow."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ValueReference:
    """A data lookup used while rendering a workflow template."""

    source: str
    step_id: StepId | None = None
    field: str = ""


@dataclass(frozen=True, slots=True)
class TemplateBinding:
    name: str
    reference: ValueReference


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    """A format string whose named values are resolved from durable run state."""

    text: str
    bindings: tuple[TemplateBinding, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Transition:
    """One edge to another step or to a terminal workflow outcome."""

    step_id: StepId | None = None
    terminal: TerminalOutcome | None = None


@dataclass(frozen=True, slots=True)
class OutcomeTransition:
    outcome: str
    transition: Transition


@dataclass(frozen=True, slots=True)
class AgentStep:
    step_id: StepId
    name: str
    profile: AgentProfile
    prompt: WorkflowTemplate
    transitions: tuple[OutcomeTransition, ...]
    required_outputs: tuple[str, ...] = field(default=())
    editable: bool = False
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ

    @property
    def spec(self) -> StepSpec:
        return StepSpec(
            step_id=self.step_id,
            agent_id=self.profile.agent_id,
            required_outputs=self.required_outputs,
            editable=self.editable,
        )


@dataclass(frozen=True, slots=True)
class HumanReviewStep:
    step_id: StepId
    name: str
    title: WorkflowTemplate
    summary: WorkflowTemplate
    approved: Transition
    rejected: Transition


WorkflowStep = AgentStep | HumanReviewStep


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    base_ref: str = ""
    """Explicit Git ref, or empty to use the configured default branch."""


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A complete, serialisable, sequential/branching workflow graph."""

    workflow_id: WorkflowId
    name: str
    version: str
    steps: tuple[WorkflowStep, ...]
    workspace: WorkspaceSpec = WorkspaceSpec()
    naming_profile: AgentProfile | None = None
    naming_prompt: str = ""

    @property
    def entry_step_id(self) -> StepId:
        return self.steps[0].step_id

    def step(self, step_id: StepId) -> WorkflowStep | None:
        return next((step for step in self.steps if step.step_id == step_id), None)


__all__ = [
    "AgentStep",
    "HumanReviewStep",
    "OutcomeTransition",
    "StepOutput",
    "StepSpec",
    "TemplateBinding",
    "TerminalOutcome",
    "Transition",
    "ValueReference",
    "WorkflowDefinition",
    "WorkflowStep",
    "WorkflowTemplate",
    "WorkspaceAccess",
    "WorkspaceSpec",
]
