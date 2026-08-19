"""Catalog-driven run read model."""

from dataclasses import dataclass, field

from engine.core.workflow_interpreter import render_template
from engine.domain import (
    AgentId,
    AgentInstance,
    AgentInstanceId,
    AgentRunId,
    AgentStep,
    ConversationId,
    HumanReviewStep,
    RunId,
    RunPhase,
    RunState,
    StepCompleted,
    StepId,
    StepOutput,
    WorkflowDefinition,
)
from engine.ports.state_store import StateStore
from engine.runtime.workflows import WorkflowCatalog


@dataclass(frozen=True, slots=True)
class RunStepView:
    step_id: StepId
    name: str
    kind: str
    status: str
    outcome: str | None = None
    summary: str = ""
    outputs: tuple[StepOutput, ...] = field(default=())
    changes_requested: bool = False
    agent_id: AgentId | None = None
    agent_instance_id: AgentInstanceId | None = None
    agent_run_id: AgentRunId | None = None
    mcp_request_id: str | int | None = None
    conversation_id: ConversationId | None = None


@dataclass(frozen=True, slots=True)
class PendingHumanReviewView:
    step_id: StepId
    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class HumanDecisionView:
    step_id: StepId
    approved: bool
    outcome: str
    summary: str


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    run_id: RunId
    name: str
    workflow_id: str
    workflow_name: str
    workflow_version: str
    task_id: str
    task_prompt: str
    repository: str
    phase: str
    current_step_id: StepId | None
    terminal_outcome: str | None
    steps: tuple[RunStepView, ...]
    pending_human_review: PendingHumanReviewView | None = None
    human_decision: HumanDecisionView | None = None
    failure_reason: str = ""


class RunReader:
    """Build complete run views from durable state and its definition snapshot."""

    def __init__(self, store: StateStore, catalog: WorkflowCatalog | None = None) -> None:
        self._store = store
        self._catalog = (
            catalog
            if catalog is not None
            else WorkflowCatalog.from_definitions(())
        )

    async def list(self) -> tuple[WorkflowRunView, ...]:
        return tuple([await self._view(state) for state in await self._store.list_runs()])

    async def get(self, run_id: RunId) -> WorkflowRunView | None:
        state = await self._store.load(run_id)
        return await self._view(state) if state is not None else None

    async def _view(self, state: RunState) -> WorkflowRunView:
        definition = state.workflow_definition or self._catalog.get(state.workflow_id)
        instances = await self._store.list_instances(workflow_run_id=state.run_id)
        by_step = {
            instance.workflow_step_id: instance
            for instance in instances
            if instance.workflow_step_id is not None
        }
        results = {result.step_id: result for result in state.step_results}
        steps = (
            tuple(
                _step_view(state, step, by_step.get(step.step_id), results.get(step.step_id))
                for step in definition.steps
            )
            if definition is not None
            else ()
        )
        pending = _pending_human_review(state, definition)
        decision = None
        if state.human_review is not None:
            decision = HumanDecisionView(
                step_id=state.human_review.step_id,
                approved=state.human_review.approved,
                outcome="approved" if state.human_review.approved else "rejected",
                summary=state.human_review.summary,
            )
        return WorkflowRunView(
            run_id=state.run_id,
            name=state.name or state.prompt or str(state.run_id),
            workflow_id=str(state.workflow_id),
            workflow_name=definition.name if definition is not None else str(state.workflow_id),
            workflow_version=definition.version if definition is not None else "",
            task_id=str(state.task_id),
            task_prompt=state.prompt,
            repository=state.repository,
            phase=state.phase.value,
            current_step_id=state.current_step_id,
            terminal_outcome=_terminal_outcome(state),
            steps=steps,
            pending_human_review=pending,
            human_decision=decision,
            failure_reason=state.failure_reason,
        )


def _step_view(
    state: RunState,
    step: AgentStep | HumanReviewStep,
    instance: AgentInstance | None,
    result: StepCompleted | None,
) -> RunStepView:
    if isinstance(step, HumanReviewStep):
        if state.human_review is not None and state.human_review.step_id == step.step_id:
            status = "completed"
            outcome = "approved" if state.human_review.approved else "rejected"
            summary = state.human_review.summary
        elif state.phase is RunPhase.AWAITING_HUMAN_REVIEW and state.current_step_id == step.step_id:
            status, outcome, summary = "action_required", None, "Human decision required."
        else:
            status, outcome, summary = _step_status(state, step.step_id, False), None, ""
        return RunStepView(
            step_id=step.step_id,
            name=step.name,
            kind="human",
            status=status,
            outcome=outcome,
            summary=summary,
        )
    status = _step_status(state, step.step_id, result is not None)
    return RunStepView(
        step_id=step.step_id,
        name=step.name,
        kind="agent",
        status=status,
        outcome=result.outcome if result is not None else None,
        summary=(
            result.summary
            if result is not None
            else state.failure_reason if status == "failed" else ""
        ),
        outputs=result.outputs if result is not None else (),
        changes_requested=result is not None and result.outcome == "changes_requested",
        agent_id=step.profile.agent_id,
        agent_instance_id=instance.instance_id if instance else None,
        agent_run_id=(
            result.agent_run_id
            if result is not None
            else state.current_agent_run_id if state.current_step_id == step.step_id else None
        ),
        mcp_request_id=result.mcp_request_id if result is not None else None,
        conversation_id=instance.conversation_id if instance else None,
    )


def _pending_human_review(
    state: RunState, definition: WorkflowDefinition | None
) -> PendingHumanReviewView | None:
    if state.phase is not RunPhase.AWAITING_HUMAN_REVIEW or definition is None:
        return None
    step = definition.step(state.current_step_id) if state.current_step_id else None
    if not isinstance(step, HumanReviewStep):
        return None
    return PendingHumanReviewView(
        step_id=step.step_id,
        title=render_template(step.title, state),
        summary=render_template(step.summary, state),
    )


def _step_status(state: RunState, step_id: StepId, completed: bool) -> str:
    if completed:
        return "completed"
    if state.current_step_id != step_id:
        return "pending"
    if state.phase is RunPhase.FAILED:
        return "failed"
    if state.phase is RunPhase.AWAITING_HUMAN_REVIEW:
        return "action_required"
    if state.is_terminal:
        return "completed"
    return "in_progress"


def _terminal_outcome(state: RunState) -> str | None:
    if state.human_review is not None:
        return "approved" if state.human_review.approved else "rejected"
    if state.phase is RunPhase.SUCCEEDED:
        return "succeeded"
    if state.phase is RunPhase.FAILED:
        return "failed"
    return None


__all__ = [
    "HumanDecisionView",
    "PendingHumanReviewView",
    "RunReader",
    "RunStepView",
    "WorkflowRunView",
]
