"""The foreman: a planner agent wired to real execution.

A planner is an ordinary agent on an ordinary `AgentRunner`. What makes it a
foreman is this module: its tool calls are translated into domain events, folded
through `engine.core.planning.decide_plan`, and the commands that come back
start real workers on the same port.

The important consequence is that the model never mutates the plan. It calls
`dispatch_task`; the engine decides whether that dispatch is legal (dependencies
met, not already running) and only then does a worker start. A confused planner
produces a refused tool call, not a corrupt plan.

Workers get `WORKER_TOOLS` -- no `dispatch_task` -- so delegation is one level
deep by construction.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from engine.core.planning import decide_plan
from engine.domain.commands import Command, StartAttempt
from engine.domain.events import (
    Event,
    GoalSet,
    TaskAdded,
    TaskDispatchRequested,
    TaskFinished,
    TaskStarted,
)
from engine.domain.ids import AgentId, AttemptId, PlanId, RunId, TaskId
from engine.domain.planning import Plan, TaskStatus
from engine.ports.agent_runner import (
    AgentEvent,
    AgentRunner,
    AgentSpec,
    TextDelta,
    Thinking,
    ToolCallFinished,
    ToolCallStarted,
    ToolResult,
    ToolSpec,
    TurnFinished,
)
from engine.runtime.filesystem import Workspace, invoke_filesystem_tool
from engine.runtime.tools import PLANNER_TOOLS, WORKER_TOOLS

PLANNER_SYSTEM_PROMPT = """\
You are a foreman. You break work into tasks, hand them to workers, and report back.

How you operate:
- Start by calling set_goal, then add_task for each piece of work. Add the whole
  plan before dispatching anything, so dependencies are complete.
- Workers see only the task's detail field. They cannot see this conversation, so
  each brief must stand alone: paths, constraints, and what done looks like.
- Dispatch every ready task in the same turn so they run in parallel, then call
  await_tasks once to collect the results.
- A task that is too vague for a stranger to execute is too big. Split it.

You do not do the work yourself; you have no file access. If a request is small
enough that a single worker can just do it, make it one task rather than
inventing structure around it.

Keep your messages to the user short. Say what you are about to do before you do
it, report what actually happened after, and lead with the outcome rather than
the process. When you report results, report the plan's real state -- call
list_tasks rather than trusting your memory of it.
"""

WORKER_SYSTEM_PROMPT = """\
You are executing one task inside a shared workspace.

Do exactly the task you were given and nothing adjacent to it. You cannot see the
wider plan or talk to whoever assigned this, so if the brief is ambiguous, choose
the reading a careful colleague would and say which one you chose in your report.

Read a file before you overwrite it -- write_file replaces the whole file.

Call report exactly once when you are finished or blocked. Your summary is the
only thing that reaches the planner.
"""


# --- what the UI sees -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForemanEvent:
    """Base class for everything a subscriber observes."""


@dataclass(frozen=True, slots=True)
class PlannerText(ForemanEvent):
    text: str


@dataclass(frozen=True, slots=True)
class PlannerThinking(ForemanEvent):
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ToolActivity(ForemanEvent):
    name: str
    arguments: Mapping[str, object]
    result: str = ""
    is_error: bool = False
    finished: bool = False


@dataclass(frozen=True, slots=True)
class WorkerText(ForemanEvent):
    task_id: TaskId
    text: str


@dataclass(frozen=True, slots=True)
class PlanUpdated(ForemanEvent):
    plan: Plan


@dataclass(frozen=True, slots=True)
class TurnEnded(ForemanEvent):
    stop_reason: str = "end_turn"


@dataclass(frozen=True, slots=True)
class ForemanError(ForemanEvent):
    message: str


class _Broadcast:
    """Fan-out to every live subscriber. Slow subscribers do not block others."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[ForemanEvent | None]] = []

    def publish(self, event: ForemanEvent) -> None:
        for queue in list(self._queues):
            queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[ForemanEvent]:
        queue: asyncio.Queue[ForemanEvent | None] = asyncio.Queue()
        self._queues.append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            if queue in self._queues:
                self._queues.remove(queue)

    def close(self) -> None:
        for queue in list(self._queues):
            queue.put_nowait(None)


@dataclass
class _WorkerRun:
    task_id: TaskId
    attempt_id: AttemptId
    task: asyncio.Task[None]
    succeeded: bool = False
    summary: str = ""
    reported: bool = field(default=False)


class Foreman:
    """A planner agent plus the machinery that makes its decisions real."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        run_id: RunId,
        plan_id: PlanId,
        workspace: Workspace,
        model: str | None = None,
        planner_tools: Sequence[ToolSpec] = PLANNER_TOOLS,
        worker_tools: Sequence[ToolSpec] = WORKER_TOOLS,
    ) -> None:
        self._runner = runner
        self._run_id = run_id
        self._workspace = workspace
        self._model = model
        self._worker_tools = tuple(worker_tools)
        self._plan = Plan(plan_id=plan_id)
        self._events = _Broadcast()
        self._workers: dict[TaskId, _WorkerRun] = {}
        self._session = runner.start(
            AgentSpec(
                agent_id=AgentId(f"planner-{run_id}"),
                system_prompt=PLANNER_SYSTEM_PROMPT,
                tools=tuple(planner_tools),
                model=model,
            ),
            self._invoke_planner_tool,
        )

    @property
    def plan(self) -> Plan:
        return self._plan

    def subscribe(self) -> AsyncIterator[ForemanEvent]:
        """The UI's feed. Every planner turn and every worker pushes here."""
        return self._events.subscribe()

    async def send(self, message: str) -> None:
        """Run one planner turn to completion, publishing as it goes."""
        try:
            async for event in self._session.send(message):
                self._publish_agent_event(event)
        except Exception as error:  # a backend failure must not kill the UI
            self._events.publish(ForemanError(f"{type(error).__name__}: {error}"))
            self._events.publish(TurnEnded(stop_reason="error"))

    async def close(self) -> None:
        for worker in list(self._workers.values()):
            worker.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker.task
        await self._session.close()
        self._events.close()

    # --- planner tool handling ---------------------------------------------

    def _publish_agent_event(self, event: AgentEvent) -> None:
        match event:
            case TextDelta(text=text):
                self._events.publish(PlannerText(text))
            case Thinking(summary=summary):
                self._events.publish(PlannerThinking(summary))
            case ToolCallStarted(name=name, arguments=arguments):
                self._events.publish(ToolActivity(name=name, arguments=arguments))
            case ToolCallFinished(name=name, result=result):
                self._events.publish(
                    ToolActivity(
                        name=name,
                        arguments={},
                        result=result.content,
                        is_error=result.is_error,
                        finished=True,
                    )
                )
            case TurnFinished(stop_reason=stop_reason):
                self._events.publish(TurnEnded(stop_reason))

    async def _invoke_planner_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> ToolResult:
        match name:
            case "set_goal":
                goal = str(arguments.get("goal", "")).strip()
                if not goal:
                    return ToolResult("goal must not be empty.", is_error=True)
                self._apply(GoalSet(run_id=self._run_id, plan_id=self._plan.plan_id, goal=goal))
                return ToolResult(f"Goal set: {goal}")

            case "add_task":
                return self._add_task(arguments)

            case "dispatch_task":
                return await self._dispatch_task(arguments)

            case "await_tasks":
                return await self._await_tasks(arguments)

            case "list_tasks":
                return ToolResult(render_plan(self._plan))

            case _:
                return ToolResult(f"Unknown tool {name!r}.", is_error=True)

    def _add_task(self, arguments: Mapping[str, object]) -> ToolResult:
        task_id = str(arguments.get("task_id", "")).strip()
        title = str(arguments.get("title", "")).strip()
        if not task_id or not title:
            return ToolResult("task_id and title are both required.", is_error=True)
        if self._plan.task(TaskId(task_id)) is not None:
            return ToolResult(f"Task {task_id!r} already exists.", is_error=True)

        raw_depends = arguments.get("depends_on") or ()
        depends_on = tuple(TaskId(str(d)) for d in raw_depends)  # type: ignore[union-attr]
        unknown = [d for d in depends_on if self._plan.task(d) is None]
        if unknown:
            return ToolResult(
                f"Unknown dependencies: {', '.join(unknown)}. Add those tasks first.",
                is_error=True,
            )

        self._apply(
            TaskAdded(
                run_id=self._run_id,
                task_id=TaskId(task_id),
                title=title,
                detail=str(arguments.get("detail", "")),
                depends_on=depends_on,
            )
        )
        task = self._plan.task(TaskId(task_id))
        assert task is not None
        return ToolResult(f"Added {task_id} ({task.status.value}): {title}")

    async def _dispatch_task(self, arguments: Mapping[str, object]) -> ToolResult:
        task_id = TaskId(str(arguments.get("task_id", "")).strip())
        task = self._plan.task(task_id)
        if task is None:
            return ToolResult(f"No task {task_id!r}. Call add_task first.", is_error=True)
        if task_id in self._workers:
            return ToolResult(f"{task_id} is already running.", is_error=True)
        if task.is_terminal:
            return ToolResult(f"{task_id} already finished ({task.status.value}).", is_error=True)
        if not self._plan.dependencies_met(task):
            outstanding = [
                d
                for d in task.depends_on
                if (dep := self._plan.task(d)) is None or dep.status is not TaskStatus.DONE
            ]
            return ToolResult(
                f"{task_id} is blocked on {', '.join(outstanding)}. "
                "Await those first.",
                is_error=True,
            )

        instructions = str(arguments.get("instructions", "")).strip()
        self._apply(
            TaskDispatchRequested(
                run_id=self._run_id, task_id=task_id, instructions=instructions
            )
        )
        if task_id not in self._workers:
            return ToolResult(f"{task_id} was not dispatched.", is_error=True)
        return ToolResult(f"{task_id} dispatched. Call await_tasks to collect the result.")

    async def _await_tasks(self, arguments: Mapping[str, object]) -> ToolResult:
        raw_ids = arguments.get("task_ids") or []
        wanted = (
            [TaskId(str(t)) for t in raw_ids]  # type: ignore[union-attr]
            if raw_ids
            else list(self._workers)
        )
        pending = [self._workers[t] for t in wanted if t in self._workers]
        if not pending:
            return ToolResult("Nothing is running.", is_error=True)

        await asyncio.gather(
            *(w.task for w in pending), return_exceptions=True
        )

        lines = []
        for worker in pending:
            task = self._plan.task(worker.task_id)
            status = task.status.value if task else "unknown"
            lines.append(f"{worker.task_id} [{status}]: {worker.summary or '(no summary)'}")
            self._workers.pop(worker.task_id, None)
        return ToolResult("\n".join(lines))

    # --- engine plumbing ----------------------------------------------------

    def _apply(self, event: Event) -> None:
        """Fold an event into the plan and act on whatever the engine returns."""
        self._plan, commands = decide_plan(self._plan, event)
        self._events.publish(PlanUpdated(self._plan))
        for command in commands:
            self._execute(command)

    def _execute(self, command: Command) -> None:
        match command:
            case StartAttempt(attempt_id=attempt_id, prompt=prompt, task_id=task_id):
                if task_id is None:
                    return
                run = _WorkerRun(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    task=asyncio.create_task(
                        self._run_worker(task_id, attempt_id, prompt)
                    ),
                )
                self._workers[task_id] = run
            case _:
                # Other commands belong to the full runtime dispatcher; the
                # foreman only owns agent execution.
                return

    async def _run_worker(self, task_id: TaskId, attempt_id: AttemptId, prompt: str) -> None:
        self._apply(TaskStarted(run_id=self._run_id, task_id=task_id, attempt_id=attempt_id))
        worker = self._workers.get(task_id)

        async def invoke(name: str, arguments: Mapping[str, object]) -> ToolResult:
            if name == "report":
                if worker is not None:
                    worker.succeeded = bool(arguments.get("succeeded", False))
                    worker.summary = str(arguments.get("summary", ""))
                    worker.reported = True
                return ToolResult("Report recorded.")
            result = await invoke_filesystem_tool(self._workspace, name, arguments)
            if result is None:
                return ToolResult(f"Unknown tool {name!r}.", is_error=True)
            return result

        session = self._runner.start(
            AgentSpec(
                agent_id=AgentId(f"worker-{task_id}"),
                system_prompt=WORKER_SYSTEM_PROMPT,
                tools=self._worker_tools,
                model=self._model,
            ),
            invoke,
        )
        try:
            async for event in session.send(prompt):
                if isinstance(event, TextDelta):
                    self._events.publish(WorkerText(task_id, event.text))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if worker is not None:
                worker.succeeded = False
                worker.summary = f"Worker failed: {type(error).__name__}: {error}"
        finally:
            with contextlib.suppress(Exception):
                await session.close()

        if worker is not None and not worker.reported and not worker.summary:
            # An agent that stops without calling report has not done the task
            # as specified; recording that is more useful than guessing success.
            worker.summary = "Worker ended without calling report."

        self._apply(
            TaskFinished(
                run_id=self._run_id,
                task_id=task_id,
                succeeded=bool(worker and worker.succeeded),
                summary=worker.summary if worker else "",
            )
        )


def render_plan(plan: Plan) -> str:
    """The plan as the planner sees it through `list_tasks`."""
    if not plan.tasks:
        return f"Goal: {plan.goal or '(not set)'}\nNo tasks yet."
    lines = [f"Goal: {plan.goal or '(not set)'}", ""]
    for task in plan.tasks:
        deps = f" after {', '.join(task.depends_on)}" if task.depends_on else ""
        lines.append(f"[{task.status.value}] {task.task_id}: {task.title}{deps}")
        if task.result:
            lines.append(f"    -> {task.result}")
    return "\n".join(lines)


__all__ = [
    "PLANNER_SYSTEM_PROMPT",
    "WORKER_SYSTEM_PROMPT",
    "Foreman",
    "ForemanError",
    "ForemanEvent",
    "PlanUpdated",
    "PlannerText",
    "PlannerThinking",
    "ToolActivity",
    "TurnEnded",
    "WorkerText",
    "render_plan",
]
