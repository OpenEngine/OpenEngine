"""Execute the locally supported portion of a workflow run."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from engine.core import decide
from engine.core.workflows.implementation_review import (
    IMPLEMENTATION_STEP,
    start_review_command,
)
from engine.domain import (
    Command,
    Event,
    HumanReviewCompleted,
    ProvisionWorkspace,
    RequestHumanReview,
    RunFailed,
    RunId,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepCompleted,
    WorkspaceProvisioned,
)
from engine.ports import AgentRunner
from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher
from engine.runtime.step_results import requests_clarification_or_escalation


class WorkflowExecutionError(RuntimeError):
    """The reducer emitted a command the local execution slice cannot handle."""


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    """One executed step's terminal event, and what the reducer made of it."""

    event: Event
    state: RunState
    commands: tuple[Command, ...]


class WorkflowExecutor:
    """Drive request, workspace, implementation, and review.

    Agent execution stops where a person has to decide. Human-review ingress
    then calls ``complete_human_review`` so the same reducer owns the terminal
    transition too.

    Two runner mappings, keyed by the same provider names. `runners` may write
    inside the workspace and implements; `review_runners` may only read it and
    reviews what was implemented. The review profile also says not to modify
    anything, but an instruction the model may ignore is not a control -- the
    reviewer is handed a runner that cannot edit the tree in the first place.
    """

    def __init__(
        self,
        capabilities: Capabilities,
        runners: Mapping[str, AgentRunner] | None = None,
        *,
        review_runners: Mapping[str, AgentRunner],
    ) -> None:
        self._capabilities = capabilities
        self._dispatcher = Dispatcher(capabilities)
        self._runners = dict(runners or {"default": capabilities.agent_runner})
        # Every implementation runner must have an explicitly wired reviewer.
        # A missing reviewer is a wiring mistake, and finding it at startup is
        # better than silently reviewing with write access.
        self._review_runners = dict(review_runners)
        unreviewable = sorted(set(self._runners) - set(self._review_runners))
        if unreviewable:
            raise WorkflowExecutionError(
                f"no review runner for: {', '.join(unreviewable)}"
            )

    @property
    def runners(self) -> tuple[str, ...]:
        return tuple(self._runners)

    @property
    def default_runner(self) -> str:
        return next(iter(self._runners))

    async def advance_through_review(
        self, initial_event: RunRequested, runner_name: str = ""
    ) -> None:
        """Advance a persisted pending request through implementation and review."""
        try:
            selected_name = runner_name or self.default_runner
            try:
                runner = self._runners[selected_name]
                reviewer = self._review_runners[selected_name]
            except KeyError as error:
                raise WorkflowExecutionError(
                    f"unknown workflow runner: {selected_name}"
                ) from error
            state = await self._require_state(initial_event.run_id)
            state, commands = await self._transition(
                state, initial_event, append_event=False
            )
            provision = _only(commands, ProvisionWorkspace)
            workspace = await self._capabilities.workspace_provider.provision(
                provision.repository, provision.base_ref
            )

            state, commands = await self._transition(
                state,
                WorkspaceProvisioned(
                    run_id=state.run_id,
                    workspace_id=workspace.workspace_id,
                    root_path=workspace.root_path,
                ),
            )
            implementation = await self._run_step(
                state,
                _only(commands, StartAgentRun),
                runner=runner,
                runner_name=selected_name,
            )
            if implementation is None:
                # The step paused for clarification. Its conversation is
                # durable and a later response can resume the same instance.
                return
            if implementation.state.phase is RunPhase.FAILED:
                return
            if implementation.state.phase is not RunPhase.REVIEWING:
                raise WorkflowExecutionError(
                    "implementation result did not advance the run to review"
                )
            review = await self._run_step(
                implementation.state,
                _only(implementation.commands, StartAgentRun),
                runner=reviewer,
                runner_name=selected_name,
            )
            if review is None or review.state.phase is RunPhase.FAILED:
                return
            if review.state.phase is not RunPhase.AWAITING_HUMAN_REVIEW:
                raise WorkflowExecutionError(
                    "review result did not advance the run to human review"
                )
            # Nothing to dispatch: the persisted AWAITING_HUMAN_REVIEW
            # transition *is* the request for a human. Requiring the command
            # keeps the executor and reducer contract explicit.
            _only(review.commands, RequestHumanReview)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(initial_event.run_id, error)

    async def resume_review(self, run_id: RunId, runner_name: str = "") -> None:
        """Resume the review command lost when its process stopped.

        A successful implementation and the resulting ``REVIEWING`` state are
        durable, but command dispatch is process-local. Reconstructing the
        deterministic command lets startup finish that transition without
        rerunning the implementation.
        """
        try:
            selected_name = runner_name or self.default_runner
            try:
                reviewer = self._review_runners[selected_name]
            except KeyError as error:
                raise WorkflowExecutionError(
                    f"unknown workflow runner: {selected_name}"
                ) from error
            state = await self._require_state(run_id)
            if state.phase is not RunPhase.REVIEWING:
                return
            implementation = next(
                (
                    result
                    for result in reversed(state.step_results)
                    if result.step_id == IMPLEMENTATION_STEP
                ),
                None,
            )
            if implementation is None:
                raise WorkflowExecutionError(
                    "reviewing run has no implementation result"
                )
            review = await self._run_step(
                state,
                start_review_command(state, implementation),
                runner=reviewer,
                runner_name=selected_name,
            )
            if review is None or review.state.phase is RunPhase.FAILED:
                return
            if review.state.phase is not RunPhase.AWAITING_HUMAN_REVIEW:
                raise WorkflowExecutionError(
                    "review result did not advance the run to human review"
                )
            _only(review.commands, RequestHumanReview)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(run_id, error)

    async def complete_human_review(
        self, event: HumanReviewCompleted
    ) -> RunState:
        """Persist the final human decision and finish the workflow run."""
        state = await self._require_state(event.run_id)
        if (
            state.phase is not RunPhase.AWAITING_HUMAN_REVIEW
            or event.step_id != state.current_step_id
        ):
            raise WorkflowExecutionError("run is not awaiting human review")
        next_state, commands = await self._transition(state, event)
        if commands:
            raise WorkflowExecutionError(
                "human review unexpectedly emitted follow-up commands"
            )
        return next_state

    async def _run_step(
        self,
        state: RunState,
        command: StartAgentRun,
        *,
        runner: AgentRunner,
        runner_name: str,
    ) -> _StepOutcome | None:
        """Run one agent-backed step and fold its terminal result into the run.

        None means the agent validly paused without completing the step. Its
        conversation is durable and the run stays on this step, so a later
        response can resume the same logical instance.
        """
        assert command.step is not None
        folded: _StepOutcome | None = None

        async def deliver_terminal(event: Event) -> None:
            nonlocal folded
            next_state, commands = await self._transition(state, event)
            folded = _StepOutcome(event, next_state, commands)

        terminal = await self._dispatcher.run_workflow_agent(
            command,
            runner=runner,
            runner_name=runner_name,
            on_terminal_result=deliver_terminal,
        )
        if folded is not None:
            # The MCP acknowledgement was sent only after this transition
            # persisted the event. Never infer delivery from CLI transcript.
            assert terminal == folded.event
            return folded
        if isinstance(terminal, (StepCompleted, RunFailed)):
            next_state, commands = await self._transition(state, terminal)
            return _StepOutcome(terminal, next_state, commands)
        if requests_clarification_or_escalation(terminal):
            return None
        raise WorkflowExecutionError(
            f"{command.step.step_id} runner exited without a valid completion state"
        )

    async def _transition(
        self,
        state: RunState,
        event: Event,
        *,
        append_event: bool = True,
    ) -> tuple[RunState, tuple[Command, ...]]:
        next_state, commands = decide(state, event)
        if append_event:
            await self._capabilities.state_store.append_events(state.run_id, (event,))
        await self._capabilities.state_store.save(next_state)
        return next_state, commands

    async def _require_state(self, run_id: RunId) -> RunState:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            raise WorkflowExecutionError(f"run not found: {run_id}")
        return state

    async def _fail(self, run_id: RunId, error: Exception) -> None:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            return
        failure = RunFailed(
            run_id=run_id,
            reason=f"{type(error).__name__}: {error}",
        )
        await self._transition(state, failure)


def _only(
    commands: Sequence[Command], expected: type[Command]
) -> Command:
    if len(commands) != 1 or not isinstance(commands[0], expected):
        names = ", ".join(type(command).__name__ for command in commands) or "none"
        raise WorkflowExecutionError(
            f"expected one {expected.__name__} command, got {names}"
        )
    return commands[0]


__all__ = ["WorkflowExecutionError", "WorkflowExecutor"]
