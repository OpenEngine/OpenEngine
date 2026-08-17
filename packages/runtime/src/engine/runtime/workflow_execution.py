"""Execute the locally supported portion of a workflow run."""

import asyncio
from collections.abc import Mapping, Sequence

from engine.core import decide
from engine.domain import (
    Command,
    Event,
    ProvisionWorkspace,
    RunFailed,
    RunId,
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


class WorkflowExecutor:
    """Drive request, workspace, and implementation, stopping at review.

    Reviewer execution and human-review ingress are deliberately not part of
    this local slice yet. The reducer still owns every transition, so adding
    those effects later does not require a second workflow definition.
    """

    def __init__(
        self,
        capabilities: Capabilities,
        runners: Mapping[str, AgentRunner] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._dispatcher = Dispatcher(capabilities)
        self._runners = dict(runners or {"default": capabilities.agent_runner})

    @property
    def runners(self) -> tuple[str, ...]:
        return tuple(self._runners)

    @property
    def default_runner(self) -> str:
        return next(iter(self._runners))

    async def advance_to_review(
        self, initial_event: RunRequested, runner_name: str = ""
    ) -> None:
        """Advance a persisted pending request through implementation."""
        try:
            selected_name = runner_name or self.default_runner
            try:
                runner = self._runners[selected_name]
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
            implementation = _only(commands, StartAgentRun)
            delivered: Event | None = None

            async def deliver_terminal(event: Event) -> None:
                nonlocal state, delivered
                state, _commands = await self._transition(state, event)
                delivered = event

            terminal = await self._dispatcher.run_workflow_agent(
                implementation,
                runner=runner,
                runner_name=selected_name,
                on_terminal_result=deliver_terminal,
            )
            if delivered is not None:
                # The MCP acknowledgement was sent only after this transition
                # persisted the event. Never infer delivery from CLI transcript.
                assert terminal == delivered
                return
            if isinstance(terminal, (StepCompleted, RunFailed)):
                await self._transition(state, terminal)
                return
            if requests_clarification_or_escalation(terminal):
                # The agent validly paused without completing the step. Its
                # conversation is durable and the workflow stays IMPLEMENTING
                # so a later response can resume the same logical instance.
                return
            raise WorkflowExecutionError(
                "workflow runner exited without a valid completion state"
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(initial_event.run_id, error)

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
