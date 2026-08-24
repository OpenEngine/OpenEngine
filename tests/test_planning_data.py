"""Project, milestone, and workstream persistence contracts."""

import asyncio
from collections.abc import Iterator

import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import (
    Milestone,
    MilestoneId,
    Project,
    ProjectId,
    RunId,
    RunRequested,
    RunState,
    TaskId,
    WorkflowId,
    Workstream,
    WorkstreamId,
)
from engine.ports import StateStore


@pytest.fixture(params=("memory", "sqlite"))
def store(request: pytest.FixtureRequest) -> Iterator[StateStore]:
    value: StateStore
    if request.param == "memory":
        value = InMemoryStateStore()
    else:
        value = SQLiteStateStore(":memory:")
    yield value
    if isinstance(value, SQLiteStateStore):
        value.close()


def test_planning_hierarchy_and_run_association(store: StateStore) -> None:
    project = Project(ProjectId("project-engine"), "Engine")
    milestone = Milestone(
        MilestoneId("milestone-foundation"),
        project.project_id,
        "Foundation",
        "Establish the data and runtime foundations.",
    )
    launch = Milestone(
        MilestoneId("milestone-launch"),
        project.project_id,
        "Launch",
        "Ship the first release.",
        (milestone.milestone_id,),
    )
    workstream = Workstream(
        WorkstreamId("workstream-data"), milestone.milestone_id, "Data model"
    )
    other_workstream = Workstream(
        WorkstreamId("workstream-ui"), milestone.milestone_id, "User interface"
    )
    run = RunState(
        run_id=RunId("run-data-model"),
        task_id=TaskId("task-data-model"),
        workflow_id=WorkflowId("implementation-v1"),
        workstream_id=workstream.workstream_id,
    )

    async def scenario() -> None:
        await store.save_project(project)
        await store.save_milestone(milestone)
        await store.save_milestone(launch)
        await store.save_workstream(workstream)
        await store.save_workstream(other_workstream)
        await store.save(run)

        assert await store.load_project(project.project_id) == project
        assert await store.load_milestone(milestone.milestone_id) == milestone
        assert await store.load_workstream(workstream.workstream_id) == workstream
        assert await store.list_milestones(project.project_id) == (launch, milestone)
        assert await store.list_workstreams(milestone.milestone_id) == (
            other_workstream,
            workstream,
        )
        assert await store.list_runs(workstream.workstream_id) == (run,)
        assert await store.list_runs(other_workstream.workstream_id) == ()
        assert await store.delete_milestone(launch.milestone_id) is True
        assert await store.delete_milestone(launch.milestone_id) is False
        with pytest.raises(ValueError, match="still has workstreams"):
            await store.delete_milestone(milestone.milestone_id)

    asyncio.run(scenario())


def test_sqlite_planning_hierarchy_survives_reopening(tmp_path) -> None:
    path = tmp_path / "planning.sqlite3"
    project = Project(ProjectId("project-engine"), "Engine")
    foundation = Milestone(
        MilestoneId("milestone-foundation"), project.project_id, "Foundation"
    )
    milestone = Milestone(
        MilestoneId("milestone-v1"),
        project.project_id,
        "V1",
        "The first usable release.",
        (foundation.milestone_id,),
    )
    workstream = Workstream(
        WorkstreamId("workstream-runtime"), milestone.milestone_id, "Runtime"
    )
    run = RunState(
        run_id=RunId("run-runtime"),
        task_id=TaskId("task-runtime"),
        workflow_id=WorkflowId("implementation-v1"),
        workstream_id=workstream.workstream_id,
    )
    requested = RunRequested(
        run_id=run.run_id,
        task_id=run.task_id,
        prompt="Implement runtime support.",
        repository="openai/openengine",
        workflow_id=run.workflow_id,
        workstream_id=workstream.workstream_id,
    )

    first = SQLiteStateStore(path)
    asyncio.run(first.save_project(project))
    asyncio.run(first.save_milestone(foundation))
    asyncio.run(first.save_milestone(milestone))
    asyncio.run(first.save_workstream(workstream))
    asyncio.run(first.save(run))
    asyncio.run(first.append_events(run.run_id, (requested,)))
    first.close()

    second = SQLiteStateStore(path)
    try:
        assert asyncio.run(second.list_projects()) == (project,)
        assert asyncio.run(second.list_milestones()) == (milestone, foundation)
        assert asyncio.run(second.list_workstreams()) == (workstream,)
        assert asyncio.run(second.load(run.run_id)) == run
        assert asyncio.run(second.history(run.run_id)) == (requested,)
    finally:
        second.close()


def test_planning_children_require_their_parent(store: StateStore) -> None:
    missing_project = ProjectId("project-missing")
    missing_milestone = MilestoneId("milestone-missing")

    with pytest.raises(KeyError, match="no project"):
        asyncio.run(
            store.save_milestone(
                Milestone(MilestoneId("milestone-orphan"), missing_project, "Orphan")
            )
        )
    with pytest.raises(KeyError, match="no milestone"):
        asyncio.run(
            store.save_workstream(
                Workstream(
                    WorkstreamId("workstream-orphan"), missing_milestone, "Orphan"
                )
            )
        )
