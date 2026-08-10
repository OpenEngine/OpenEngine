"""Plan decisions.

The same shape as `decide`: pure, synchronous, `(state, event) -> (state, commands)`.
The planner agent proposes -- by calling a tool -- and this disposes. Putting the
rules here rather than in the tool handlers is what stops the plan from becoming
whatever the model last said:

- a task can only be dispatched once,
- a task cannot be dispatched before its dependencies are done,
- a finished task cannot be reopened by a later duplicate event.

None of that needs an LLM, so none of it is left to one.
"""

from dataclasses import replace

from engine.domain.commands import Command, StartAttempt
from engine.domain.events import (
    Event,
    GoalSet,
    TaskAdded,
    TaskDispatchRequested,
    TaskFinished,
    TaskStarted,
)
from engine.domain.ids import AttemptId, WorkspaceId
from engine.domain.planning import Plan, PlanTask, TaskStatus


class PlanDecision(tuple[Plan, tuple[Command, ...]]):
    """Result of `decide_plan`: the next plan plus commands to dispatch."""

    __slots__ = ()

    def __new__(cls, plan: Plan, commands: tuple[Command, ...]) -> "PlanDecision":
        return super().__new__(cls, (plan, commands))

    @property
    def plan(self) -> Plan:
        return self[0]

    @property
    def commands(self) -> tuple[Command, ...]:
        return self[1]


def decide_plan(plan: Plan, event: Event) -> PlanDecision:
    """Fold one planning event into the plan.

    Total: an event that does not apply is a no-op, so a confused planner cannot
    wedge the run by emitting something unexpected.
    """
    match event:
        case GoalSet(goal=goal):
            return PlanDecision(replace(plan, goal=goal), ())

        case TaskAdded(task_id=task_id, title=title, detail=detail, depends_on=depends_on):
            if plan.task(task_id) is not None:
                return PlanDecision(plan, ())  # re-adding an existing task is a no-op
            task = PlanTask(
                task_id=task_id,
                title=title,
                detail=detail,
                depends_on=tuple(depends_on),
            )
            # Record up front whether it is blocked, so the UI can say so without
            # recomputing the dependency graph.
            next_plan = plan.with_task(task)
            if not next_plan.dependencies_met(task):
                next_plan = next_plan.with_task(replace(task, status=TaskStatus.BLOCKED))
            return PlanDecision(next_plan, ())

        case TaskDispatchRequested(run_id=run_id, task_id=task_id, instructions=instructions):
            task = plan.task(task_id)
            if task is None or task.status not in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                return PlanDecision(plan, ())  # already running, or already done
            if not plan.dependencies_met(task):
                return PlanDecision(plan.with_task(replace(task, status=TaskStatus.BLOCKED)), ())
            attempt_id = AttemptId(f"{task_id}-attempt-1")
            dispatched = replace(task, status=TaskStatus.DISPATCHED, attempt_id=attempt_id)
            return PlanDecision(
                plan.with_task(dispatched),
                (
                    StartAttempt(
                        run_id=run_id,
                        attempt_id=attempt_id,
                        workspace_id=WorkspaceId(f"ws-{run_id}"),
                        prompt=instructions or _worker_prompt(task),
                        task_id=task_id,
                    ),
                ),
            )

        case TaskStarted(task_id=task_id, attempt_id=attempt_id):
            task = plan.task(task_id)
            if task is None or task.is_terminal:
                return PlanDecision(plan, ())
            return PlanDecision(
                plan.with_task(
                    replace(task, status=TaskStatus.RUNNING, attempt_id=attempt_id)
                ),
                (),
            )

        case TaskFinished(task_id=task_id, succeeded=succeeded, summary=summary):
            task = plan.task(task_id)
            if task is None or task.is_terminal:
                return PlanDecision(plan, ())
            finished = replace(
                task,
                status=TaskStatus.DONE if succeeded else TaskStatus.FAILED,
                result=summary,
            )
            next_plan = plan.with_task(finished)
            if succeeded:
                next_plan = _unblock_dependents(next_plan)
            return PlanDecision(next_plan, ())

        case _:
            return PlanDecision(plan, ())


def _unblock_dependents(plan: Plan) -> Plan:
    """Move blocked tasks back to pending once their dependencies land."""
    for task in plan.tasks:
        if task.status is TaskStatus.BLOCKED and plan.dependencies_met(task):
            plan = plan.with_task(replace(task, status=TaskStatus.PENDING))
    return plan


def _worker_prompt(task: PlanTask) -> str:
    """The default brief handed to a worker when the planner supplies none.

    Subagents see none of the planner's conversation, so the brief has to carry
    the task on its own.
    """
    lines = [task.title]
    if task.detail:
        lines.append("")
        lines.append(task.detail)
    return "\n".join(lines)


__all__ = ["PlanDecision", "decide_plan"]
