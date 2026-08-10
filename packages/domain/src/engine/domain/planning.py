"""The plan a foreman works from.

Pure data, like the rest of `domain`. A `Plan` is what the planner has decided
should happen; it is not a record of what an agent said. The planner's tool calls
become events, events fold into this, and this is what the UI renders.
"""

from dataclasses import dataclass, field, replace
from enum import Enum

from engine.domain.ids import AttemptId, PlanId, TaskId


class TaskStatus(Enum):
    """Where one task has got to."""

    PENDING = "pending"
    BLOCKED = "blocked"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED)


@dataclass(frozen=True, slots=True)
class PlanTask:
    """One unit of work the foreman can hand to a worker."""

    task_id: TaskId
    title: str
    detail: str = ""
    status: TaskStatus = TaskStatus.PENDING
    depends_on: tuple[TaskId, ...] = field(default=())
    attempt_id: AttemptId | None = None
    result: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


@dataclass(frozen=True, slots=True)
class Plan:
    """The foreman's working set.

    Task order is insertion order -- the planner decides sequencing through
    `depends_on`, not through list position.
    """

    plan_id: PlanId
    goal: str = ""
    tasks: tuple[PlanTask, ...] = field(default=())

    def task(self, task_id: TaskId) -> PlanTask | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def with_task(self, task: PlanTask) -> "Plan":
        """Add the task, or replace the existing one with the same id."""
        if self.task(task.task_id) is None:
            return replace(self, tasks=(*self.tasks, task))
        return replace(
            self,
            tasks=tuple(task if t.task_id == task.task_id else t for t in self.tasks),
        )

    def dependencies_met(self, task: PlanTask) -> bool:
        """True when every task this one waits on has finished successfully."""
        for dependency_id in task.depends_on:
            dependency = self.task(dependency_id)
            if dependency is None or dependency.status is not TaskStatus.DONE:
                return False
        return True

    @property
    def ready(self) -> tuple[PlanTask, ...]:
        """Pending tasks whose dependencies are satisfied -- dispatchable now."""
        return tuple(
            t
            for t in self.tasks
            if t.status is TaskStatus.PENDING and self.dependencies_met(t)
        )

    @property
    def is_complete(self) -> bool:
        return bool(self.tasks) and all(t.is_terminal for t in self.tasks)

    def counts(self) -> dict[str, int]:
        """Per-status tally, for status lines and the UI header."""
        tally = {status.value: 0 for status in TaskStatus}
        for task in self.tasks:
            tally[task.status.value] += 1
        return tally


__all__ = ["Plan", "PlanTask", "TaskStatus"]
