"""Command dispatch: the one place where decisions become effects.

The engine emits commands. This module is the *only* code that turns them into
calls on real capabilities. Keeping that translation in a single, small,
exhaustive `match` is what lets the boundary be checked mechanically -- if a
command has no arm here, dispatch fails loudly rather than silently doing
nothing.

Ticket 1 ships the seam and its wiring; the per-command bodies fill in alongside
their adapters.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace

from engine.domain.agents import AgentRun, AgentRunStatus
from engine.domain.chat import Message
from engine.domain.commands import (
    Command,
    Notify,
    PersistRun,
    ProvisionWorkspace,
    PublishChanges,
    RequestHumanReview,
    ScheduleTimer,
    StartAgentRun,
)
from engine.domain.events import RunFailed, StepCompleted
from engine.domain.ids import ConversationId
from engine.ports import AgentRunner, AgentTurn, McpAgentRunner
from engine.runtime.capabilities import Capabilities
from engine.runtime.step_results import step_result_instructions
from engine.runtime.terminal_mcp import (
    TerminalEvent,
    TerminalMcpBroker,
    TerminalResultRegistry,
)


class UnhandledCommandError(RuntimeError):
    """A command reached dispatch with no capability mapped to it."""

    def __init__(self, command: Command) -> None:
        super().__init__(f"no capability handles {type(command).__name__}")
        self.command = command


class Dispatcher:
    """Executes engine commands against a wired capability set."""

    def __init__(self, capabilities: Capabilities) -> None:
        self._capabilities = capabilities
        self._terminal_results = TerminalResultRegistry()

    async def dispatch_all(self, commands: Iterable[Command]) -> None:
        """Dispatch in order. Sequential by design -- commands from one decision
        may depend on each other, and ordering is the engine's to choose."""
        for command in commands:
            await self.dispatch(command)

    async def dispatch(self, command: Command) -> None:
        caps = self._capabilities
        match command:
            case ProvisionWorkspace():
                await caps.workspace_provider.provision(command.repository, command.base_ref)
            case StartAgentRun():
                if command.step is None:
                    await caps.agent_runner.run_turn(
                        command.agent_run_id,
                        command.profile,
                        (Message.user(command.prompt),),
                        workspace_id=command.workspace_id,
                    )
                else:
                    await self.run_workflow_agent(command)
            case RequestHumanReview():
                # The state store is the durable request. A future ingress may
                # additionally notify an external review system.
                pass
            case PublishChanges():
                await caps.source_control.publish(command.workspace_id, command.branch)
            case Notify():
                await caps.communications.post(command.channel, command.message, command.run_id)
            case PersistRun():
                # Fills in once the runtime threads state through dispatch.
                pass
            case ScheduleTimer():
                await caps.workflow_runtime.schedule_timer(
                    command.run_id, command.delay_seconds, command.reason
                )
            case _:
                raise UnhandledCommandError(command)

    async def run_workflow_agent(
        self,
        command: StartAgentRun,
        runner: AgentRunner | None = None,
        runner_name: str = "",
        on_terminal_result: Callable[[TerminalEvent], Awaitable[None]] | None = None,
    ) -> AgentTurn | TerminalEvent:
        """Run a workflow step, preferring a directly delivered MCP result."""
        caps = self._capabilities
        selected_runner = runner or caps.agent_runner
        assert command.step is not None
        instance = await caps.state_store.create_instance(
            command.profile.agent_id,
            workspace_id=command.workspace_id,
            instance_id=command.instance_id,
            conversation_id=ConversationId(f"{command.instance_id}:conversation"),
            workflow_run_id=command.run_id,
            workflow_step_id=command.step.step_id,
            runner=runner_name,
        )
        conversation = await caps.state_store.load_conversation(instance.instance_id)
        prompt = Message.user(
            f"{command.prompt}\n\n{step_result_instructions(command.step)}"
        )
        if conversation is not None and not conversation.messages:
            await caps.state_store.append_messages(instance.instance_id, (prompt,))

        agent_run = AgentRun(
            agent_run_id=command.agent_run_id,
            instance_id=instance.instance_id,
            status=AgentRunStatus.RUNNING,
            runner=runner_name,
        )
        await caps.state_store.record_agent_run(agent_run)
        try:
            if isinstance(selected_runner, McpAgentRunner):
                result, turn = await self._run_with_terminal_mcp(
                    selected_runner,
                    command,
                    prompt,
                    on_terminal_result,
                )
            else:
                result = None
                turn = await selected_runner.run_turn(
                    command.agent_run_id,
                    command.profile,
                    (prompt,),
                    workspace_id=command.workspace_id,
                )
        except Exception as error:
            await caps.state_store.record_agent_run(
                replace(
                    agent_run,
                    status=AgentRunStatus.FAILED,
                    summary=f"{type(error).__name__}: {error}",
                )
            )
            raise
        if turn is not None:
            await caps.state_store.append_messages(instance.instance_id, turn.transcript)
        if result is not None:
            status = (
                AgentRunStatus.FAILED
                if isinstance(result, RunFailed)
                else AgentRunStatus.SUCCEEDED
            )
            summary = result.reason if isinstance(result, RunFailed) else result.summary
            await caps.state_store.record_agent_run(
                replace(agent_run, status=status, summary=summary)
            )
            return result
        assert turn is not None
        await caps.state_store.record_agent_run(
            replace(
                agent_run,
                status=AgentRunStatus.SUCCEEDED,
                summary=turn.message.content,
            )
        )
        return turn

    async def _run_with_terminal_mcp(
        self,
        runner: McpAgentRunner,
        command: StartAgentRun,
        prompt: Message,
        deliver: Callable[[TerminalEvent], Awaitable[None]] | None,
    ) -> tuple[TerminalEvent | None, AgentTurn | None]:
        """Race natural CLI exit against the bound terminal tool submission."""
        assert command.step is not None
        broker = TerminalMcpBroker(
            run_id=command.run_id,
            agent_run_id=command.agent_run_id,
            step=command.step,
            registry=self._terminal_results,
            deliver=deliver,
        )
        async with broker:
            run_task = asyncio.create_task(
                runner.run_turn_with_mcp(
                    command.agent_run_id,
                    command.profile,
                    (prompt,),
                    broker.config,
                    workspace_id=command.workspace_id,
                )
            )
            result_task = asyncio.create_task(broker.result())
            done, _pending = await asyncio.wait(
                (run_task, result_task), return_when=asyncio.FIRST_COMPLETED
            )
            if result_task in done:
                result = result_task.result()
                try:
                    await runner.cancel(command.agent_run_id)
                except Exception:
                    # Cancellation is best-effort and cannot revoke a result
                    # already accepted and delivered by the runtime.
                    pass
                turn: AgentTurn | None = None
                try:
                    turn = await asyncio.wait_for(run_task, timeout=1.0)
                except asyncio.TimeoutError:
                    run_task.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
                except Exception:
                    # A terminated CLI commonly reports a nonzero exit. The
                    # already-delivered terminal event remains authoritative.
                    pass
                return result, turn

            result_task.cancel()
            await asyncio.gather(result_task, return_exceptions=True)
            return None, await run_task


__all__ = ["Dispatcher", "UnhandledCommandError"]
