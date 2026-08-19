"""Execute compiled sequential/branching workflows with durable transitions."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from engine.core import decide
from engine.core.workflow_interpreter import current_agent_command
from engine.domain import (
    AgentRunId,
    AgentStep,
    Command,
    Event,
    HumanReviewCompleted,
    Message,
    ProvisionWorkspace,
    RequestHumanReview,
    RunFailed,
    RunId,
    RunNamed,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepCompleted,
    WorkflowDefinition,
    WorkspaceAccess,
    WorkspaceProvisioned,
)
from engine.ports import AgentRunner, ApprovalHandler, InteractiveMcpAgentRunner
from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher
from engine.runtime.step_results import requests_clarification_or_escalation
from engine.runtime.workflows import WorkflowCatalog


class WorkflowExecutionError(RuntimeError):
    """The workflow or local composition cannot execute the requested transition."""


@dataclass(frozen=True, slots=True)
class _StepOutcome:
    event: Event
    state: RunState
    commands: tuple[Command, ...]


class WorkflowExecutor:
    """Drive a compiled workflow until it pauses or reaches a terminal state."""

    def __init__(
        self,
        capabilities: Capabilities,
        runners: Mapping[str, AgentRunner] | None = None,
        *,
        review_runners: Mapping[str, AgentRunner],
        approval_handler: Callable[[StartAgentRun, str], ApprovalHandler] | None = None,
        catalog: WorkflowCatalog | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._dispatcher = Dispatcher(capabilities)
        self._runners = dict(runners or {"default": capabilities.agent_runner})
        self._review_runners = dict(review_runners)
        self._approval_handler = approval_handler
        self._catalog = (
            catalog
            if catalog is not None
            else WorkflowCatalog.from_definitions(())
        )
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

    @property
    def catalog(self) -> WorkflowCatalog:
        return self._catalog

    async def advance_through_review(
        self, initial_event: RunRequested, runner_name: str = ""
    ) -> None:
        """Compatibility name: start and drive any configured workflow."""

        try:
            selected_name = self._runner_name(runner_name)
            state = await self._require_state(initial_event.run_id)
            definition = self._definition_for(state, initial_event.workflow_id)
            if state.workflow_definition is None:
                state = replace(state, workflow_definition=definition)
                await self._capabilities.state_store.save(state)
            state, commands = await self._transition(
                state, initial_event, definition, append_event=False
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
                definition,
            )
            state = await self._name_workflow(state, definition, selected_name)
            await self._drive(state, commands, definition, selected_name)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(initial_event.run_id, error)

    async def resume_agent_step(
        self,
        run_id: RunId,
        message: str | None = None,
        runner_name: str = "",
    ) -> None:
        """Reconstruct and run the current agent command after a pause/restart."""

        try:
            selected_name = self._runner_name(runner_name)
            state = await self._require_state(run_id)
            definition = self._definition_for(state)
            if state.phase not in (
                RunPhase.RUNNING_AGENT,
                RunPhase.IMPLEMENTING,
                RunPhase.REVIEWING,
            ):
                raise WorkflowExecutionError("run is not executing an agent step")
            step = definition.step(state.current_step_id) if state.current_step_id else None
            if not isinstance(step, AgentStep):
                raise WorkflowExecutionError("current workflow step is not an agent step")
            if message is not None and not step.editable:
                raise WorkflowExecutionError("workflow step is read-only")
            command = current_agent_command(definition, state)
            runner = self._runner_for(step, selected_name)
            outcome = await self._run_step(
                state,
                command,
                definition=definition,
                runner=runner,
                runner_name=selected_name,
                continuation=message,
            )
            if outcome is not None:
                await self._drive(
                    outcome.state,
                    outcome.commands,
                    definition,
                    selected_name,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(run_id, error)

    async def resume_implementation(
        self, run_id: RunId, message: str, runner_name: str = ""
    ) -> None:
        await self.resume_agent_step(run_id, message, runner_name)

    async def resume_review(self, run_id: RunId, runner_name: str = "") -> None:
        await self.resume_agent_step(run_id, None, runner_name)

    async def complete_human_review(
        self, event: HumanReviewCompleted
    ) -> RunState:
        state = await self._require_state(event.run_id)
        if (
            state.phase is not RunPhase.AWAITING_HUMAN_REVIEW
            or event.step_id != state.current_step_id
        ):
            raise WorkflowExecutionError("run is not awaiting human review")
        definition = self._definition_for(state)
        next_state, commands = await self._transition(state, event, definition)
        if commands:
            # v1 permits a human branch to another sequential step. Run it in
            # the background caller's selected/default provider.
            await self._drive(
                next_state, commands, definition, self.default_runner
            )
            return await self._require_state(event.run_id)
        return next_state

    async def _drive(
        self,
        state: RunState,
        commands: tuple[Command, ...],
        definition: WorkflowDefinition,
        runner_name: str,
    ) -> RunState:
        while commands:
            if len(commands) != 1:
                raise WorkflowExecutionError("parallel workflow commands are not supported in v1")
            command = commands[0]
            if isinstance(command, RequestHumanReview):
                return state
            if not isinstance(command, StartAgentRun) or command.step is None:
                raise WorkflowExecutionError(
                    f"unsupported workflow command: {type(command).__name__}"
                )
            step = definition.step(command.step.step_id)
            if not isinstance(step, AgentStep):
                raise WorkflowExecutionError(f"agent step not found: {command.step.step_id}")
            outcome = await self._run_step(
                state,
                command,
                definition=definition,
                runner=self._runner_for(step, runner_name),
                runner_name=runner_name,
            )
            if outcome is None:
                return state
            state, commands = outcome.state, outcome.commands
        return state

    async def _name_workflow(
        self,
        state: RunState,
        definition: WorkflowDefinition,
        runner_name: str,
    ) -> RunState:
        if definition.naming_profile is None or not definition.naming_prompt:
            return state
        try:
            turn = await self._runners[runner_name].run_turn(
                AgentRunId(f"{state.run_id}:name:run"),
                definition.naming_profile,
                (Message.user(state.prompt), Message.user(definition.naming_prompt)),
                workspace_id=state.workspace_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return state
        name = _clean_workflow_name(turn.message.content)
        if not name:
            return state
        named, commands = await self._transition(
            state, RunNamed(run_id=state.run_id, name=name), definition
        )
        if commands:
            raise WorkflowExecutionError("naming a workflow emitted commands")
        return named

    async def _run_step(
        self,
        state: RunState,
        command: StartAgentRun,
        *,
        definition: WorkflowDefinition,
        runner: AgentRunner,
        runner_name: str,
        continuation: str | None = None,
    ) -> _StepOutcome | None:
        assert command.step is not None
        folded: _StepOutcome | None = None

        async def deliver_terminal(event: Event) -> None:
            nonlocal folded
            next_state, commands = await self._transition(state, event, definition)
            folded = _StepOutcome(event, next_state, commands)

        terminal = await self._dispatcher.run_workflow_agent(
            command,
            runner=runner,
            runner_name=runner_name,
            on_terminal_result=deliver_terminal,
            on_approval=(
                self._approval_handler(command, runner_name)
                if self._approval_handler is not None
                and isinstance(runner, InteractiveMcpAgentRunner)
                else None
            ),
            continuation=continuation,
        )
        if folded is not None:
            assert terminal == folded.event
            return folded
        if isinstance(terminal, (StepCompleted, RunFailed)):
            next_state, commands = await self._transition(state, terminal, definition)
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
        definition: WorkflowDefinition,
        *,
        append_event: bool = True,
    ) -> tuple[RunState, tuple[Command, ...]]:
        next_state, commands = decide(state, event, definition)
        if append_event:
            await self._capabilities.state_store.append_events(state.run_id, (event,))
        await self._capabilities.state_store.save(next_state)
        return next_state, commands

    def _definition_for(
        self, state: RunState, requested_id=None
    ) -> WorkflowDefinition:
        if state.workflow_definition is not None:
            return state.workflow_definition
        workflow_id = requested_id or state.workflow_id
        try:
            return self._catalog.require(workflow_id)
        except ValueError as error:
            raise WorkflowExecutionError(str(error)) from error

    def _runner_name(self, runner_name: str) -> str:
        selected = runner_name or self.default_runner
        if selected not in self._runners:
            raise WorkflowExecutionError(f"unknown workflow runner: {selected}")
        return selected

    def _runner_for(self, step: AgentStep, runner_name: str) -> AgentRunner:
        mapping = (
            self._runners
            if step.workspace_access is WorkspaceAccess.WRITE
            else self._review_runners
        )
        try:
            return mapping[runner_name]
        except KeyError as error:
            raise WorkflowExecutionError(
                f"no {step.workspace_access.value} runner for: {runner_name}"
            ) from error

    async def _require_state(self, run_id: RunId) -> RunState:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            raise WorkflowExecutionError(f"run not found: {run_id}")
        return state

    async def _fail(self, run_id: RunId, error: Exception) -> None:
        state = await self._capabilities.state_store.load(run_id)
        if state is None:
            return
        definition = self._definition_for(state)
        failure = RunFailed(run_id=run_id, reason=f"{type(error).__name__}: {error}")
        await self._transition(state, failure, definition)


def _only(commands: Sequence[Command], expected: type[Command]) -> Command:
    if len(commands) != 1 or not isinstance(commands[0], expected):
        names = ", ".join(type(command).__name__ for command in commands) or "none"
        raise WorkflowExecutionError(
            f"expected one {expected.__name__} command, got {names}"
        )
    return commands[0]


def _clean_workflow_name(value: str) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    return first_line.strip(" \t\"'`).:;!?")[:80]


__all__ = ["WorkflowExecutionError", "WorkflowExecutor"]
