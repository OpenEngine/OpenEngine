"""The planner is an agent; the engine is what makes it a foreman.

These tests exist to pin the claim the architecture rests on: a planner and a
worker are the same thing on the same port, differing only in their tools, and
the *plan* is decided by code rather than by whatever the model last said.

Everything runs on the scripted runner -- no network, no credentials.
"""

import asyncio
from pathlib import Path

import pytest

from engine.adapters.scripted import (
    DEMO_SCRIPT,
    Script,
    ScriptedAgentRunner,
    ScriptedToolCall,
    ScriptedTurn,
)
from engine.core.planning import decide_plan
from engine.domain.events import (
    GoalSet,
    RunRequested,
    TaskAdded,
    TaskDispatchRequested,
    TaskFinished,
)
from engine.domain.ids import PlanId, RunId, TaskId
from engine.domain.planning import Plan, PlanTask, TaskStatus
from engine.runtime import PLANNER_TOOLS, WORKER_TOOLS, Foreman, Workspace

RUN = RunId("run-test")
PLAN = PlanId("plan-test")


def build(runner: ScriptedAgentRunner, tmp_path: Path) -> Foreman:
    return Foreman(runner, run_id=RUN, plan_id=PLAN, workspace=Workspace(tmp_path))


def drive(foreman: Foreman, message: str) -> None:
    async def run() -> None:
        await foreman.send(message)
        await foreman.close()

    asyncio.run(run())


# --- the distinction itself -------------------------------------------------


def test_planner_and_worker_differ_only_in_tools() -> None:
    """Neither list is a subset of the other by accident -- check the shape."""
    planner = {t.name for t in PLANNER_TOOLS}
    worker = {t.name for t in WORKER_TOOLS}
    assert "dispatch_task" in planner
    assert planner.isdisjoint(worker), "a tool in both lists blurs the roles"


def test_a_worker_cannot_dispatch_work(tmp_path: Path) -> None:
    """Recursion is prevented by construction, not by an instruction.

    A worker that *could* dispatch would only be stopped by its prompt. Removing
    the tool means a runaway delegation tree is not expressible.
    """
    runner = ScriptedAgentRunner(DEMO_SCRIPT)
    drive(build(runner, tmp_path), "go")

    workers = runner.specs_for("worker")
    assert workers, "no worker ran"
    for spec in workers:
        names = {t.name for t in spec.tools}
        assert "dispatch_task" not in names
        assert "add_task" not in names


def test_planner_gets_planning_tools_and_no_file_access(tmp_path: Path) -> None:
    runner = ScriptedAgentRunner(DEMO_SCRIPT)
    drive(build(runner, tmp_path), "go")

    planner = runner.specs_for("planner")[0]
    names = {t.name for t in planner.tools}
    assert "dispatch_task" in names
    assert {"read_file", "write_file"}.isdisjoint(names)


# --- end to end -------------------------------------------------------------


def test_foreman_plans_dispatches_and_finishes(tmp_path: Path) -> None:
    runner = ScriptedAgentRunner(DEMO_SCRIPT)
    foreman = build(runner, tmp_path)
    drive(foreman, "Write me a brief and a README.")

    plan = foreman.plan
    assert plan.goal
    assert [t.task_id for t in plan.tasks] == ["brief", "readme"]
    assert all(t.status is TaskStatus.DONE for t in plan.tasks)
    assert plan.is_complete


def test_workers_do_real_work_in_the_workspace(tmp_path: Path) -> None:
    """The dispatched worker writes files -- dispatch is not bookkeeping."""
    drive(build(ScriptedAgentRunner(DEMO_SCRIPT), tmp_path), "go")

    assert (tmp_path / "BRIEF.md").is_file()
    assert (tmp_path / "README.md").is_file()


def test_dependent_task_runs_after_its_dependency(tmp_path: Path) -> None:
    """`readme` depends on `brief` and reads the file `brief` wrote."""
    runner = ScriptedAgentRunner(DEMO_SCRIPT)
    drive(build(runner, tmp_path), "go")

    order = [s.agent_id for s in runner.started if s.agent_id.startswith("worker")]
    assert order.index("worker-brief") < order.index("worker-readme")
    assert "foreman" in (tmp_path / "README.md").read_text()


# --- the engine overrules the model -----------------------------------------


def _planner_script(*calls: ScriptedToolCall) -> dict[str, Script]:
    return {"planner": Script(turns=(ScriptedTurn(text="ok", tool_calls=calls),))}


def test_dispatching_a_blocked_task_is_refused(tmp_path: Path) -> None:
    """A model asking for the wrong thing gets an error, not a broken plan."""
    runner = ScriptedAgentRunner(
        _planner_script(
            ScriptedToolCall("add_task", {"task_id": "a", "title": "First"}),
            ScriptedToolCall(
                "add_task", {"task_id": "b", "title": "Second", "depends_on": ["a"]}
            ),
            ScriptedToolCall("dispatch_task", {"task_id": "b"}),  # a is not done
        )
    )
    foreman = build(runner, tmp_path)
    drive(foreman, "go")

    b = foreman.plan.task(TaskId("b"))
    assert b is not None and b.status is TaskStatus.BLOCKED
    assert not runner.specs_for("worker"), "a blocked task must not start a worker"


def test_unknown_dependency_is_refused(tmp_path: Path) -> None:
    runner = ScriptedAgentRunner(
        _planner_script(
            ScriptedToolCall(
                "add_task", {"task_id": "b", "title": "Second", "depends_on": ["ghost"]}
            )
        )
    )
    foreman = build(runner, tmp_path)
    drive(foreman, "go")
    assert foreman.plan.task(TaskId("b")) is None


def test_duplicate_task_is_refused(tmp_path: Path) -> None:
    runner = ScriptedAgentRunner(
        _planner_script(
            ScriptedToolCall("add_task", {"task_id": "a", "title": "First"}),
            ScriptedToolCall("add_task", {"task_id": "a", "title": "Different title"}),
        )
    )
    foreman = build(runner, tmp_path)
    drive(foreman, "go")

    assert len(foreman.plan.tasks) == 1
    assert foreman.plan.tasks[0].title == "First"


def test_dispatching_an_unknown_task_is_refused(tmp_path: Path) -> None:
    runner = ScriptedAgentRunner(
        _planner_script(ScriptedToolCall("dispatch_task", {"task_id": "nope"}))
    )
    foreman = build(runner, tmp_path)
    drive(foreman, "go")
    assert foreman.plan.tasks == ()
    assert not runner.specs_for("worker")


# --- decide_plan is pure ----------------------------------------------------


def test_decide_plan_is_pure_and_total() -> None:
    plan = Plan(plan_id=PLAN)
    event = TaskAdded(run_id=RUN, task_id=TaskId("a"), title="First")

    assert decide_plan(plan, event) == decide_plan(plan, event)
    assert plan.tasks == (), "the input plan must not be mutated"

    # An event the plan knows nothing about is a no-op, not an error.
    unrelated = RunRequested(
        run_id=RUN, task_id=TaskId("t"), prompt="p", repository="acme/api"
    )
    assert decide_plan(plan, unrelated).plan is plan


def test_finishing_a_dependency_unblocks_its_dependents() -> None:
    plan = Plan(plan_id=PLAN)
    plan, _ = decide_plan(plan, GoalSet(run_id=RUN, plan_id=PLAN, goal="g"))
    plan, _ = decide_plan(plan, TaskAdded(run_id=RUN, task_id=TaskId("a"), title="A"))
    plan, _ = decide_plan(
        plan,
        TaskAdded(run_id=RUN, task_id=TaskId("b"), title="B", depends_on=(TaskId("a"),)),
    )
    assert plan.task(TaskId("b")).status is TaskStatus.BLOCKED

    plan, _ = decide_plan(
        plan, TaskFinished(run_id=RUN, task_id=TaskId("a"), succeeded=True, summary="ok")
    )
    assert plan.task(TaskId("b")).status is TaskStatus.PENDING
    assert [t.task_id for t in plan.ready] == ["b"]


def test_dispatch_emits_a_command_rather_than_running_anything() -> None:
    """The engine's output is still a command; the foreman is what executes it."""
    plan = Plan(plan_id=PLAN, tasks=(PlanTask(task_id=TaskId("a"), title="A"),))
    plan, commands = decide_plan(
        plan, TaskDispatchRequested(run_id=RUN, task_id=TaskId("a"))
    )

    assert len(commands) == 1
    assert type(commands[0]).__name__ == "StartAttempt"
    assert commands[0].task_id == TaskId("a")
    assert plan.task(TaskId("a")).status is TaskStatus.DISPATCHED


def test_a_finished_task_cannot_be_reopened() -> None:
    plan = Plan(
        plan_id=PLAN,
        tasks=(PlanTask(task_id=TaskId("a"), title="A", status=TaskStatus.DONE),),
    )
    after, commands = decide_plan(
        plan, TaskDispatchRequested(run_id=RUN, task_id=TaskId("a"))
    )
    assert commands == ()
    assert after.task(TaskId("a")).status is TaskStatus.DONE


# --- worker filesystem confinement ------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../escape.txt", "../../etc/passwd", "sub/../../escape.txt", "/etc/passwd"],
)
def test_worker_cannot_write_outside_the_workspace(tmp_path: Path, path: str) -> None:
    """`path` is model output. Resolve, then contain."""
    from engine.runtime.filesystem import WorkspaceEscape

    workspace = Workspace(tmp_path / "ws")
    workspace.root.mkdir(parents=True)
    with pytest.raises(WorkspaceEscape):
        workspace.resolve(path)


def test_lookalike_sibling_directory_is_not_inside(tmp_path: Path) -> None:
    """The classic string-prefix bug: /ws-evil starts with /ws."""
    from engine.runtime.filesystem import WorkspaceEscape

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws-evil").mkdir()
    workspace = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceEscape):
        workspace.resolve("../ws-evil/file.txt")


def test_workspace_round_trips_a_file(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.write_file("nested/dir/note.md", "hello")
    assert workspace.read_file("nested/dir/note.md") == "hello"
    assert "nested/dir/note.md" in workspace.list_files("**/*")


def test_escape_is_reported_to_the_agent_not_raised(tmp_path: Path) -> None:
    """A refused tool call must come back as a result the agent can read."""
    from engine.runtime.filesystem import invoke_filesystem_tool

    result = asyncio.run(
        invoke_filesystem_tool(
            Workspace(tmp_path), "write_file", {"path": "../nope", "content": "x"}
        )
    )
    assert result is not None and result.is_error
    assert "Refused" in result.content
    assert not (tmp_path.parent / "nope").exists()
