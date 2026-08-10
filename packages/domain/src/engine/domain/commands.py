"""Commands: requests for the outside world to do something.

Commands are the engine's *only* output. The engine never calls an adapter; it
returns commands and the runtime decides which port executes them. That
indirection is what keeps the engine synchronous, pure, and testable without a
single mock.

Each command names the capability that fulfils it -- see `engine.ports`.

Placeholder set for Ticket 1; the real vocabulary lands with the engine itself.
"""

from dataclasses import dataclass

from engine.domain.ids import AttemptId, RunId, TaskId, WorkspaceId


@dataclass(frozen=True, slots=True)
class Command:
    """Base class for every engine output."""

    run_id: RunId


@dataclass(frozen=True, slots=True)
class ProvisionWorkspace(Command):
    """Fulfilled by the Workspace Provider capability."""

    repository: str
    base_ref: str


@dataclass(frozen=True, slots=True)
class StartAttempt(Command):
    """Fulfilled by the Agent Runner capability.

    `task_id` is set when the attempt is a planner-dispatched task, and None for
    a plain single-shot run. One command rather than two, because it is the same
    capability doing the same thing either way -- the tools handed to the agent
    differ, not the act of running it.
    """

    attempt_id: AttemptId
    workspace_id: WorkspaceId
    prompt: str
    task_id: TaskId | None = None


@dataclass(frozen=True, slots=True)
class PublishChanges(Command):
    """Fulfilled by the Source Control capability."""

    workspace_id: WorkspaceId
    branch: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class Notify(Command):
    """Fulfilled by the Communications capability."""

    channel: str
    message: str


@dataclass(frozen=True, slots=True)
class PersistRun(Command):
    """Fulfilled by the State Store capability."""


@dataclass(frozen=True, slots=True)
class ScheduleTimer(Command):
    """Fulfilled by the Workflow Runtime capability."""

    delay_seconds: float
    reason: str


__all__ = [
    "Command",
    "Notify",
    "PersistRun",
    "ProvisionWorkspace",
    "PublishChanges",
    "ScheduleTimer",
    "StartAttempt",
]
