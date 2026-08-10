"""Events: things that have already happened.

Events are inputs to the engine. They are facts, stated in the past tense, and
are never speculative -- an adapter emits one only after the world has actually
changed. Compare `commands`, which are requests for change.

Placeholder set for Ticket 1; the real vocabulary lands with the engine itself.
"""

from dataclasses import dataclass, field

from engine.domain.ids import AttemptId, PlanId, RunId, TaskId, WorkspaceId


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
class AttemptCompleted(Event):
    """An agent runner finished an attempt, successfully or not."""

    attempt_id: AttemptId
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


# --- planning ---------------------------------------------------------------
#
# These arrive from the planner's tool calls. The foreman turns a tool call into
# an event and folds it through the engine; it never mutates the plan directly.
# That is what keeps the plan reconstructible from history alone.


@dataclass(frozen=True, slots=True)
class GoalSet(Event):
    """The planner committed to what it is trying to achieve."""

    plan_id: PlanId
    goal: str


@dataclass(frozen=True, slots=True)
class TaskAdded(Event):
    """The planner broke off a unit of work."""

    task_id: TaskId
    title: str
    detail: str = ""
    depends_on: tuple[TaskId, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class TaskDispatchRequested(Event):
    """The planner asked for a task to be executed by a worker."""

    task_id: TaskId
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class TaskStarted(Event):
    """A worker picked the task up."""

    task_id: TaskId
    attempt_id: AttemptId


@dataclass(frozen=True, slots=True)
class TaskFinished(Event):
    """A worker finished, successfully or not."""

    task_id: TaskId
    succeeded: bool
    summary: str = ""


__all__ = [
    "AttemptCompleted",
    "ChangesPublished",
    "Event",
    "GoalSet",
    "RunFailed",
    "RunRequested",
    "TaskAdded",
    "TaskDispatchRequested",
    "TaskFinished",
    "TaskStarted",
    "WorkspaceProvisioned",
]
