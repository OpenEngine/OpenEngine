"""Pure interpreter for compiled sequential/branching workflow definitions."""

from __future__ import annotations

from dataclasses import replace

from engine.domain import (
    AgentRunCompleted,
    AgentRunId,
    AgentStep,
    AgentStepPaused,
    Command,
    Event,
    HumanReviewCompleted,
    HumanReviewStep,
    ProvisionWorkspace,
    RequestHumanReview,
    RunFailed,
    RunNamed,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepCompleted,
    StepId,
    StepReactivated,
    TerminalOutcome,
    Transition,
    ValueReference,
    WorkflowDefinition,
    WorkflowTemplate,
    WorkspaceProvisioned,
)
from engine.domain.ids import AgentInstanceId


def agent_instance_id(run_id: str, step_id: StepId) -> AgentInstanceId:
    return AgentInstanceId(f"{run_id}:{step_id}:instance")


def agent_run_id(run_id: str, step_id: StepId) -> AgentRunId:
    return AgentRunId(f"{run_id}:{step_id}:run")


def render_template(template: WorkflowTemplate, state: RunState) -> str:
    values = {
        binding.name: _reference_value(binding.reference, state)
        for binding in template.bindings
    }
    return template.text.format_map(values)


def start_agent_command(
    definition: WorkflowDefinition, state: RunState, step: AgentStep
) -> StartAgentRun:
    return StartAgentRun(
        run_id=state.run_id,
        agent_run_id=(
            state.current_agent_run_id
            if state.current_step_id == step.step_id
            and state.current_agent_run_id is not None
            else agent_run_id(state.run_id, step.step_id)
        ),
        instance_id=agent_instance_id(state.run_id, step.step_id),
        profile=step.profile,
        prompt=render_template(step.prompt, state),
        workspace_id=state.workspace_id,
        step=step.spec,
    )


def current_agent_command(
    definition: WorkflowDefinition, state: RunState
) -> StartAgentRun:
    step = definition.step(state.current_step_id) if state.current_step_id else None
    if not isinstance(step, AgentStep):
        raise ValueError("current workflow step is not an agent step")
    return start_agent_command(definition, state, step)


def decide_workflow(
    definition: WorkflowDefinition, state: RunState, event: Event
) -> tuple[RunState, tuple[Command, ...]]:
    """Fold one event through a compiled workflow without performing I/O."""

    match event:
        case RunRequested() if state.phase is RunPhase.PENDING:
            next_state = replace(
                state,
                task_id=event.task_id,
                workflow_id=event.workflow_id,
                workstream_id=event.workstream_id,
                milestone_id=event.milestone_id,
                workflow_definition=definition,
                phase=RunPhase.PREPARING_WORKSPACE,
                repository=event.repository,
                prompt=event.prompt,
            )
            return next_state, (
                ProvisionWorkspace(
                    run_id=event.run_id,
                    repository=event.repository,
                    base_ref=definition.workspace.base_ref,
                ),
            )

        case WorkspaceProvisioned() if state.phase is RunPhase.PREPARING_WORKSPACE:
            prepared = replace(state, workspace_id=event.workspace_id)
            return _enter_step(definition, prepared, definition.entry_step_id)

        case RunNamed():
            return replace(state, name=event.name), ()

        case AgentStepPaused() if (
            state.phase is RunPhase.RUNNING_AGENT
            and event.step_id == state.current_step_id
            and event.agent_run_id == state.current_agent_run_id
        ):
            return replace(state, agent_paused=True), ()

        case StepReactivated():
            step = definition.step(event.step_id)
            if (
                not isinstance(step, AgentStep)
                or not step.editable
                or state.workspace_id is None
            ):
                return state, ()
            if (
                state.phase is RunPhase.RUNNING_AGENT
                and state.current_step_id == event.step_id
            ):
                return replace(state, agent_paused=False), ()
            expected_run_id = _next_agent_run_id(state, event.step_id)
            return replace(
                state,
                phase=RunPhase.RUNNING_AGENT,
                current_step_id=event.step_id,
                current_agent_run_id=expected_run_id,
                agent_paused=False,
                agent_runs=(*state.agent_runs, expected_run_id),
                step_results=(),
                human_review=None,
                human_reviews=(),
                failure_reason="",
            ), ()

        case StepCompleted() if (
            state.phase is RunPhase.RUNNING_AGENT
            and not state.agent_paused
            and event.step_id == state.current_step_id
            and event.agent_run_id == state.current_agent_run_id
        ):
            step = definition.step(event.step_id)
            if not isinstance(step, AgentStep):
                return state, ()
            with_result = replace(
                state,
                step_results=(*state.step_results, event),
            )
            transition = next(
                (
                    edge.transition
                    for edge in step.transitions
                    if edge.outcome == event.outcome
                ),
                next(
                    (
                        edge.transition
                        for edge in step.transitions
                        if edge.outcome == "*"
                    ),
                    None,
                ),
            )
            if transition is None:
                return replace(
                    with_result,
                    phase=RunPhase.FAILED,
                    failure_reason=(
                        f"step {step.step_id} produced unmapped outcome {event.outcome!r}"
                    ),
                ), ()
            return _follow(definition, with_result, transition)

        case HumanReviewCompleted() if (
            state.phase is RunPhase.AWAITING_HUMAN_REVIEW
            and event.step_id == state.current_step_id
        ):
            step = definition.step(event.step_id)
            if not isinstance(step, HumanReviewStep):
                return state, ()
            prior_reviews = state.human_reviews or (
                (state.human_review,) if state.human_review is not None else ()
            )
            reviewed = replace(
                state,
                human_review=event,
                human_reviews=(*prior_reviews, event),
            )
            return _follow(
                definition, reviewed, step.approved if event.approved else step.rejected
            )

        case AgentRunCompleted() if (
            state.phase is RunPhase.RUNNING_AGENT
            and event.agent_run_id == state.current_agent_run_id
            and not event.succeeded
        ):
            return replace(
                state, phase=RunPhase.FAILED, failure_reason=event.summary
            ), ()

        case RunFailed():
            return replace(
                state, phase=RunPhase.FAILED, failure_reason=event.reason
            ), ()

        case _:
            return state, ()


def _follow(
    definition: WorkflowDefinition, state: RunState, transition: Transition
) -> tuple[RunState, tuple[Command, ...]]:
    if transition.terminal is not None:
        phase = (
            RunPhase.SUCCEEDED
            if transition.terminal is TerminalOutcome.SUCCEEDED
            else RunPhase.FAILED
        )
        return replace(
            state,
            phase=phase,
            current_agent_run_id=None,
            agent_paused=False,
        ), ()
    assert transition.step_id is not None
    return _enter_step(definition, state, transition.step_id)


def _enter_step(
    definition: WorkflowDefinition, state: RunState, step_id: StepId
) -> tuple[RunState, tuple[Command, ...]]:
    step = definition.step(step_id)
    if isinstance(step, AgentStep):
        expected_run_id = _next_agent_run_id(state, step.step_id)
        next_state = replace(
            state,
            phase=RunPhase.RUNNING_AGENT,
            current_step_id=step.step_id,
            current_agent_run_id=expected_run_id,
            agent_paused=False,
            agent_runs=(*state.agent_runs, expected_run_id),
        )
        return next_state, (start_agent_command(definition, next_state, step),)
    if isinstance(step, HumanReviewStep):
        next_state = replace(
            state,
            phase=RunPhase.AWAITING_HUMAN_REVIEW,
            current_step_id=step.step_id,
            current_agent_run_id=None,
            agent_paused=False,
        )
        return next_state, (
            RequestHumanReview(
                run_id=state.run_id,
                step_id=step.step_id,
                title=render_template(step.title, next_state),
                summary=render_template(step.summary, next_state),
            ),
        )
    return replace(
        state,
        phase=RunPhase.FAILED,
        failure_reason=f"workflow step not found: {step_id}",
    ), ()


def _next_agent_run_id(state: RunState, step_id: StepId) -> AgentRunId:
    base = agent_run_id(state.run_id, step_id)
    attempts = sum(
        value == base or str(value).startswith(f"{base}:")
        for value in state.agent_runs
    )
    return base if attempts == 0 else AgentRunId(f"{base}:{attempts + 1}")


def _reference_value(reference: ValueReference, state: RunState) -> str:
    if reference.source == "task":
        if reference.field == "prompt":
            return state.prompt
        if reference.field == "id":
            return str(state.task_id)
        return ""
    if reference.source == "result" and reference.step_id is not None:
        result = next(
            (
                value
                for value in reversed(state.step_results)
                if value.step_id == reference.step_id
            ),
            None,
        )
        if result is None:
            return ""
        if reference.field == "outcome":
            return result.outcome
        if reference.field == "summary":
            return result.summary
        if reference.field == "outputs":
            return _outputs_text(result.outputs)
    return ""


def _outputs_text(outputs: tuple) -> str:
    if not outputs:
        return "(none)"
    return "\n".join(f"- {output.name}: {output.value}" for output in outputs)


__all__ = [
    "agent_instance_id",
    "agent_run_id",
    "current_agent_command",
    "decide_workflow",
    "render_template",
    "start_agent_command",
]
