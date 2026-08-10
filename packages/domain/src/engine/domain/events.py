"""Events: things that have already happened.

Events are inputs to the engine. They are facts, stated in the past tense, and
are never speculative -- an adapter emits one only after the world has actually
changed. Compare `commands`, which are requests for change.

Placeholder set for Ticket 1; the real vocabulary lands with the engine itself.
"""

from dataclasses import dataclass, field

from engine.domain.ids import AgentRunId, RunId, TaskId, WorkspaceId


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for every engine input."""

    run_id: RunId


@dataclass(frozen=True, slots=True)
class RunRequested(Event):
    """A human or upstream system asked for work to be done."""

    task_id: TaskId
    prompt: str
    repository: str


@dataclass(frozen=True, slots=True)
class WorkspaceProvisioned(Event):
    """A workspace provider handed back a usable checkout."""

    workspace_id: WorkspaceId
    root_path: str


@dataclass(frozen=True, slots=True)
class AgentRunCompleted(Event):
    """An agent runner finished one execution, successfully or not."""

    agent_run_id: AgentRunId
    succeeded: bool
    summary: str
    changed_files: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class ChangesPublished(Event):
    """Source control accepted the attempt's changes."""

    review_url: str


@dataclass(frozen=True, slots=True)
class RunFailed(Event):
    """An unrecoverable failure ended the run."""

    reason: str


__all__ = [
    "AgentRunCompleted",
    "ChangesPublished",
    "Event",
    "RunFailed",
    "RunRequested",
    "WorkspaceProvisioned",
]
