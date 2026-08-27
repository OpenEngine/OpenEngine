"""Public, declarative Python DSL for repository-owned OpenEngine workflows.

Workflow modules are trusted configuration: importing one executes Python once
at startup. The values produced here are immutable data; no user callback is
retained or invoked while a run is being reduced or replayed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from string import Formatter

from engine.domain import (
    AgentId,
    AgentProfile,
    AgentStep,
    HumanReviewStep,
    OutcomeTransition,
    StepId,
    TemplateBinding,
    TerminalOutcome,
    Transition,
    ValueReference,
    WorkflowDefinition,
    WorkflowId,
    WorkflowTemplate,
    WorkspaceAccess,
    WorkspaceSpec,
)


class WorkflowValidationError(ValueError):
    """A DSL value does not describe a valid v1 workflow graph."""


@dataclass(frozen=True, slots=True)
class _TaskReferences:
    prompt: ValueReference = ValueReference("task", field="prompt")
    id: ValueReference = ValueReference("task", field="id")


@dataclass(frozen=True, slots=True)
class ResultReference:
    step_id: StepId

    @property
    def outcome(self) -> ValueReference:
        return ValueReference("result", self.step_id, "outcome")

    @property
    def summary(self) -> ValueReference:
        return ValueReference("result", self.step_id, "summary")

    @property
    def outputs(self) -> ValueReference:
        return ValueReference("result", self.step_id, "outputs")


task = _TaskReferences()


def result(step_id: str) -> ResultReference:
    return ResultReference(StepId(_nonempty(step_id, "result step id")))


def template(text: str, **bindings: ValueReference) -> WorkflowTemplate:
    if not isinstance(text, str):
        raise TypeError("template text must be a string")
    compiled: list[TemplateBinding] = []
    for name, reference in bindings.items():
        if not isinstance(reference, ValueReference):
            raise TypeError(f"template binding {name!r} must be a workflow reference")
        compiled.append(TemplateBinding(name, reference))
    return WorkflowTemplate(text, tuple(compiled))


def agent(
    *,
    id: str,
    instructions: str,
    capabilities: Sequence[str] = (),
    model: str = "",
    description: str = "",
) -> AgentProfile:
    return AgentProfile(
        agent_id=AgentId(_nonempty(id, "agent id")),
        instructions=_nonempty(instructions, "agent instructions"),
        capabilities=_unique_strings(capabilities, "agent capabilities"),
        model=model,
        description=description,
    )


def workspace(*, base_ref: str | None = None) -> WorkspaceSpec:
    """Use the configured default branch unless an explicit ref is supplied."""
    if base_ref is None:
        return WorkspaceSpec()
    return WorkspaceSpec(base_ref=_nonempty(base_ref, "workspace base_ref"))


def goto(step_id: str) -> Transition:
    return Transition(step_id=StepId(_nonempty(step_id, "transition step id")))


def succeed() -> Transition:
    return Transition(terminal=TerminalOutcome.SUCCEEDED)


def fail() -> Transition:
    return Transition(terminal=TerminalOutcome.FAILED)


def agent_step(
    *,
    id: str,
    name: str,
    agent: AgentProfile,
    prompt: WorkflowTemplate,
    transitions: Mapping[str, Transition],
    required_outputs: Sequence[str] = (),
    workspace_access: str = "read",
    editable: bool = False,
) -> AgentStep:
    if not isinstance(agent, AgentProfile):
        raise TypeError("agent_step agent must be created with openengine.agent")
    if not isinstance(prompt, WorkflowTemplate):
        raise TypeError("agent_step prompt must be created with openengine.template")
    try:
        access = WorkspaceAccess(workspace_access)
    except ValueError as error:
        raise WorkflowValidationError(
            "workspace_access must be either 'read' or 'write'"
        ) from error
    edges = _transitions(transitions)
    if not edges:
        raise WorkflowValidationError("agent step must define at least one transition")
    return AgentStep(
        step_id=StepId(_nonempty(id, "step id")),
        name=_nonempty(name, "step name"),
        profile=agent,
        prompt=prompt,
        transitions=edges,
        required_outputs=_unique_strings(required_outputs, "required outputs"),
        editable=editable,
        workspace_access=access,
    )


def human_review_step(
    *,
    id: str,
    name: str,
    title: WorkflowTemplate,
    summary: WorkflowTemplate,
    approved: Transition,
    rejected: Transition,
) -> HumanReviewStep:
    if not isinstance(title, WorkflowTemplate) or not isinstance(summary, WorkflowTemplate):
        raise TypeError("human review title and summary must be workflow templates")
    _valid_transition(approved)
    _valid_transition(rejected)
    return HumanReviewStep(
        step_id=StepId(_nonempty(id, "step id")),
        name=_nonempty(name, "step name"),
        title=title,
        summary=summary,
        approved=approved,
        rejected=rejected,
    )


def workflow(
    *,
    id: str,
    name: str,
    version: str,
    steps: Sequence[AgentStep | HumanReviewStep],
    workspace: WorkspaceSpec | None = None,
    naming_agent: AgentProfile | None = None,
    naming_prompt: str = "",
) -> WorkflowDefinition:
    definition = WorkflowDefinition(
        workflow_id=WorkflowId(_nonempty(id, "workflow id")),
        name=_nonempty(name, "workflow name"),
        version=_nonempty(version, "workflow version"),
        steps=tuple(steps),
        workspace=workspace or WorkspaceSpec(),
        naming_profile=naming_agent,
        naming_prompt=naming_prompt,
    )
    validate(definition)
    return definition


def validate(definition: WorkflowDefinition) -> None:
    """Validate the deliberately sequential/branching v1 workflow shape."""

    if not isinstance(definition, WorkflowDefinition):
        raise WorkflowValidationError("exported workflow is not a WorkflowDefinition")
    if not definition.steps:
        raise WorkflowValidationError("workflow must define at least one step")
    ids = [step.step_id for step in definition.steps]
    if len(set(ids)) != len(ids):
        duplicate = next(step_id for step_id in ids if ids.count(step_id) > 1)
        raise WorkflowValidationError(f"duplicate step id: {duplicate}")
    known = set(ids)
    graph: dict[StepId, tuple[StepId, ...]] = {}
    for step in definition.steps:
        transitions = (
            tuple(edge.transition for edge in step.transitions)
            if isinstance(step, AgentStep)
            else (step.approved, step.rejected)
        )
        targets: list[StepId] = []
        for transition in transitions:
            _valid_transition(transition)
            if transition.step_id is not None:
                if transition.step_id not in known:
                    raise WorkflowValidationError(
                        f"step {step.step_id} targets missing step {transition.step_id}"
                    )
                targets.append(transition.step_id)
        graph[step.step_id] = tuple(targets)
        templates = (
            (step.prompt,)
            if isinstance(step, AgentStep)
            else (step.title, step.summary)
        )
        for value in templates:
            binding_names = [binding.name for binding in value.bindings]
            if len(set(binding_names)) != len(binding_names):
                raise WorkflowValidationError("template bindings must not contain duplicates")
            fields = [
                field_name
                for _, field_name, _, _ in Formatter().parse(value.text)
                if field_name is not None
            ]
            for field_name in fields:
                if field_name not in binding_names:
                    raise WorkflowValidationError(
                        f"template placeholder {field_name!r} has no binding"
                    )
            unused = sorted(set(binding_names) - set(fields))
            if unused:
                raise WorkflowValidationError(
                    f"template binding {unused[0]!r} is not used"
                )
            for binding in value.bindings:
                reference = binding.reference
                if reference.source not in {"task", "result"}:
                    raise WorkflowValidationError(
                        f"unknown template reference source: {reference.source}"
                    )
                if reference.source == "result" and reference.step_id not in known:
                    raise WorkflowValidationError(
                        f"template references missing step {reference.step_id}"
                    )
                allowed_fields = (
                    {"prompt", "id"}
                    if reference.source == "task"
                    else {"outcome", "summary", "outputs"}
                )
                if reference.field not in allowed_fields:
                    raise WorkflowValidationError(
                        f"unknown {reference.source} reference field: {reference.field}"
                    )

    visiting: set[StepId] = set()
    visited: set[StepId] = set()

    def visit(step_id: StepId) -> None:
        if step_id in visiting:
            raise WorkflowValidationError(f"workflow cycles are not supported in v1: {step_id}")
        if step_id in visited:
            return
        visiting.add(step_id)
        for target in graph[step_id]:
            visit(target)
        visiting.remove(step_id)
        visited.add(step_id)

    visit(definition.entry_step_id)
    unreachable = [str(step_id) for step_id in ids if step_id not in visited]
    if unreachable:
        raise WorkflowValidationError(
            f"unreachable workflow step: {', '.join(unreachable)}"
        )


def _transitions(values: Mapping[str, Transition]) -> tuple[OutcomeTransition, ...]:
    if not isinstance(values, Mapping):
        raise TypeError("transitions must be a mapping")
    compiled: list[OutcomeTransition] = []
    for outcome, transition in values.items():
        normalized = _nonempty(outcome, "transition outcome")
        _valid_transition(transition)
        compiled.append(OutcomeTransition(normalized, transition))
    return tuple(compiled)


def _valid_transition(transition: Transition) -> None:
    if not isinstance(transition, Transition):
        raise TypeError("transition must be created with goto, succeed, or fail")
    if (transition.step_id is None) == (transition.terminal is None):
        raise WorkflowValidationError(
            "transition must have exactly one step or terminal outcome"
        )


def _nonempty(value: str, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowValidationError(f"{location} must be a non-empty string")
    return value.strip()


def _unique_strings(values: Sequence[str], location: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise WorkflowValidationError(f"{location} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise WorkflowValidationError(f"{location} must not contain duplicates")
    return result


__all__ = [
    "ResultReference",
    "WorkflowValidationError",
    "agent",
    "agent_step",
    "fail",
    "goto",
    "human_review_step",
    "result",
    "succeed",
    "task",
    "template",
    "validate",
    "workflow",
    "workspace",
]
