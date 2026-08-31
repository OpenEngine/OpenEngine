"""Catalog-driven run read model."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from engine.core.workflow_interpreter import render_template
from engine.domain import (
    AgentId,
    AgentInstance,
    AgentInstanceId,
    AgentRunId,
    AgentStep,
    Conversation,
    ConversationId,
    HumanReviewCompleted,
    HumanReviewStep,
    MilestoneId,
    RunId,
    RunPhase,
    RunState,
    StepCompleted,
    StepId,
    StepOutput,
    WorkflowDefinition,
    WorkstreamId,
)
from engine.domain.approvals import ApprovalStatus
from engine.ports.state_store import StateStore
from engine.runtime.step_results import latest_turn_requests_clarification_or_escalation
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
    waiting: bool = False


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
    workstream_id: WorkstreamId | None
    milestone_id: MilestoneId | None
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
        states = tuple(await self._store.list_runs())
        instances = tuple(await self._store.list_instances())
        instances_by_run: dict[RunId, list[AgentInstance]] = {}
        for instance in instances:
            if instance.workflow_run_id is not None:
                instances_by_run.setdefault(instance.workflow_run_id, []).append(instance)
        candidates = {
            instance.instance_id
            for state in states
            if state.current_step_id is not None and not state.agent_paused
            for instance in instances_by_run.get(state.run_id, ())
            if instance.workflow_step_id == state.current_step_id
        }
        conversations = await self._store.load_conversations(tuple(candidates))
        return tuple(
            [
                await self._view(
                    state,
                    instances=instances_by_run.get(state.run_id, ()),
                    conversations=conversations,
                )
                for state in states
            ]
        )

    async def get(self, run_id: RunId) -> WorkflowRunView | None:
        state = await self._store.load(run_id)
        return await self._view(state) if state is not None else None

    async def _view(
        self,
        state: RunState,
        *,
        instances: Sequence[AgentInstance] | None = None,
        conversations: Mapping[AgentInstanceId, Conversation] | None = None,
    ) -> WorkflowRunView:
        definition = state.workflow_definition or self._catalog.get(state.workflow_id)
        if instances is None:
            instances = await self._store.list_instances(workflow_run_id=state.run_id)
        by_step = {
            instance.workflow_step_id: instance
            for instance in instances
            if instance.workflow_step_id is not None
        }
        results = {result.step_id: result for result in state.step_results}
        waiting_step = await self._waiting_step(state, by_step, conversations)
        steps = (
            tuple(
                _step_view(
                    state,
                    step,
                    by_step.get(step.step_id),
                    results.get(step.step_id),
                    waiting=waiting_step == step.step_id,
                )
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
            workflow_name=(
                definition.name if definition is not None else str(state.workflow_id)
            ),
            workflow_version=definition.version if definition is not None else "",
            task_id=str(state.task_id),
            workstream_id=state.workstream_id,
            milestone_id=state.milestone_id,
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

    async def _waiting_step(
        self,
        state: RunState,
        instances: dict[StepId, AgentInstance],
        conversations: Mapping[AgentInstanceId, Conversation] | None = None,
    ) -> StepId | None:
        step_id = state.current_step_id
        if step_id is None:
            return None
        instance = instances.get(step_id)
        if instance is None:
            return None
        if state.agent_paused:
            return step_id
        if state.current_agent_run_id is not None:
            approvals = await self._store.list_approvals(
                agent_run_id=state.current_agent_run_id,
                status=ApprovalStatus.PENDING,
            )
            if approvals:
                return step_id
        conversation = (
            conversations.get(instance.instance_id)
            if conversations is not None
            else await self._store.load_conversation(instance.instance_id)
        )
        if conversation is not None and latest_turn_requests_clarification_or_escalation(
            conversation.messages
        ):
            return step_id
        return None


def _step_view(
    state: RunState,
    step: AgentStep | HumanReviewStep,
    instance: AgentInstance | None,
    result: StepCompleted | None,
    *,
    waiting: bool = False,
) -> RunStepView:
    if isinstance(step, HumanReviewStep):
        review = _review_for(state, step.step_id)
        if review is not None:
            status = "completed"
            outcome = "approved" if review.approved else "rejected"
            summary = review.summary
        elif (
            state.phase is RunPhase.AWAITING_HUMAN_REVIEW
            and state.current_step_id == step.step_id
        ):
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
        waiting=waiting,
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
    if state.phase is RunPhase.FAILED:
        if (
            state.human_review is not None
            and not state.human_review.approved
            and state.current_step_id == state.human_review.step_id
        ):
            return "rejected"
        return "failed"
    if state.phase is not RunPhase.SUCCEEDED:
        return None
    if state.human_review is not None:
        return "approved" if state.human_review.approved else "rejected"
    return "succeeded"


def _review_for(
    state: RunState, step_id: StepId
) -> HumanReviewCompleted | None:
    return next(
        (
            review
            for review in reversed(state.human_reviews)
            if review.step_id == step_id
        ),
        state.human_review
        if state.human_review is not None and state.human_review.step_id == step_id
        else None,
    )


__all__ = [
    "HumanDecisionView",
    "PendingHumanReviewView",
    "RunReader",
    "RunStepView",
    "WorkflowRunView",
]
