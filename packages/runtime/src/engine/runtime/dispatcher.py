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

from engine.domain.commands import (
    Command,
    Notify,
    PersistRun,
    ProvisionWorkspace,
    PublishChanges,
    ScheduleTimer,
    StartAttempt,
)
from engine.runtime.capabilities import Capabilities


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
            case StartAttempt():
                await caps.agent_runner.run_attempt(
                    command.attempt_id, command.workspace_id, command.prompt
                )
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


__all__ = ["Dispatcher", "UnhandledCommandError"]
