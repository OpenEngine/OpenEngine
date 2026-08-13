"""Command dispatch: the one place where decisions become effects.

The engine emits commands. This module is the *only* code that turns them into
calls on real capabilities. Keeping that translation in a single, small,
exhaustive `match` is what lets the boundary be checked mechanically -- if a
command has no arm here, dispatch fails loudly rather than silently doing
nothing.

Ticket 1 ships the seam and its wiring; the per-command bodies fill in alongside
their adapters.
"""

from collections.abc import Iterable
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
from engine.domain.ids import ConversationId
from engine.ports import AgentRunner, AgentTurn
from engine.runtime.capabilities import Capabilities
from engine.runtime.step_results import step_result_instructions


class UnhandledCommandError(RuntimeError):
    """A command reached dispatch with no capability mapped to it."""

    def __init__(self, command: Command) -> None:
        super().__init__(f"no capability handles {type(command).__name__}")
        self.command = command


class Dispatcher:
    """Executes engine commands against a wired capability set."""

    def __init__(self, capabilities: Capabilities) -> None:
        self._capabilities = capabilities

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
    ) -> AgentTurn:
        """Run one workflow step and return its structured terminal turn."""
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
        await caps.state_store.append_messages(instance.instance_id, turn.transcript)
        await caps.state_store.record_agent_run(
            replace(
                agent_run,
                status=AgentRunStatus.SUCCEEDED,
                summary=turn.message.content,
            )
        )
        return turn


__all__ = ["Dispatcher", "UnhandledCommandError"]
