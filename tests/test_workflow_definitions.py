"""Repository-owned workflow DSL, loading, interpretation, and snapshots."""

from pathlib import Path

import pytest

import openengine as oe
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.core import decide
from engine.domain import (
    AgentId,
    AgentRunId,
    AgentStep,
    HumanReviewStep,
    HumanReviewCompleted,
    RequestHumanReview,
    RunId,
    RunPhase,
    RunRequested,
    RunState,
    StartAgentRun,
    StepCompleted,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceAccess,
    WorkspaceId,
    WorkspaceProvisioned,
)
from engine.runtime import (
    RunReader,
    WorkflowCatalog,
    WorkflowLoadError,
    load_engine_config,
    load_workflow_catalog,
)


def _definition():
    worker = oe.agent(id="worker", instructions="Do the task.")
    result = oe.result("work")
    return oe.workflow(
        id="test-v1",
        name="Test",
        version="v1",
        steps=[
            oe.agent_step(
                id="work",
                name="Work",
                agent=worker,
                prompt=oe.template("Task: {task}", task=oe.task.prompt),
                transitions={
                    "success": oe.goto("decision"),
                    "*": oe.fail(),
                },
                workspace_access="write",
            ),
            oe.human_review_step(
                id="decision",
                name="Decision",
                title=oe.template("Decide {task_id}", task_id=oe.task.id),
                summary=oe.template("Result: {summary}", summary=result.summary),
                approved=oe.succeed(),
                rejected=oe.fail(),
            ),
        ],
    )


def _multi_review_definition():
    worker = oe.agent(id="worker", instructions="Do the task.")
    return oe.workflow(
        id="multi-review-v1",
        name="Multi review",
        version="v1",
        steps=[
            oe.agent_step(
                id="work",
                name="Work",
                agent=worker,
                prompt=oe.template("Work"),
                transitions={"success": oe.goto("first-review"), "*": oe.fail()},
            ),
            oe.human_review_step(
                id="first-review",
                name="First review",
                title=oe.template("First review"),
                summary=oe.template("Check the work"),
                approved=oe.goto("verify"),
                rejected=oe.fail(),
            ),
            oe.agent_step(
                id="verify",
                name="Verify",
                agent=worker,
                prompt=oe.template("Verify"),
                transitions={"success": oe.goto("second-review"), "*": oe.fail()},
            ),
            oe.human_review_step(
                id="second-review",
                name="Second review",
                title=oe.template("Second review"),
                summary=oe.template("Check verification"),
                approved=oe.succeed(),
                rejected=oe.fail(),
            ),
        ],
    )


def _start_definition(definition):
    run_id = RunId("run-multi-review")
    task_id = TaskId("task-multi-review")
    state = RunState(run_id=run_id, task_id=task_id, workflow_id=definition.workflow_id)
    state, _ = decide(
        state,
        RunRequested(
            run_id=run_id,
            task_id=task_id,
            prompt="Ship it",
            repository="acme/repo",
            workflow_id=definition.workflow_id,
        ),
        definition,
    )
    return decide(
        state,
        WorkspaceProvisioned(
            run_id=run_id,
            workspace_id=WorkspaceId("workspace-multi-review"),
            root_path="/tmp/workspace-multi-review",
        ),
    )[0]


def test_generic_interpreter_drives_a_compiled_definition() -> None:
    definition = _definition()
    run_id = RunId("run-test")
    task_id = TaskId("task-test")
    state = RunState(run_id=run_id, task_id=task_id, workflow_id=definition.workflow_id)

    state, commands = decide(
        state,
        RunRequested(
            run_id=run_id,
            task_id=task_id,
            prompt="Ship it",
            repository="acme/repo",
            workflow_id=definition.workflow_id,
        ),
        definition,
    )
    state, commands = decide(
        state,
        WorkspaceProvisioned(
            run_id=run_id,
            workspace_id=WorkspaceId("workspace-test"),
            root_path="/tmp/workspace-test",
        ),
    )

    assert state.phase is RunPhase.RUNNING_AGENT
    assert state.workflow_definition == definition
    command = commands[0]
    assert isinstance(command, StartAgentRun)
    assert command.prompt == "Task: Ship it"

    state, commands = decide(
        state,
        StepCompleted(
            run_id=run_id,
            step_id=StepId("work"),
            agent_run_id=AgentRunId("run-test:work:run"),
            outcome="success",
            summary="Done",
        ),
    )
    assert state.phase is RunPhase.AWAITING_HUMAN_REVIEW
    assert isinstance(commands[0], RequestHumanReview)
    assert commands[0].summary == "Result: Done"

    state, commands = decide(
        state,
        HumanReviewCompleted(
            run_id=run_id,
            step_id=StepId("decision"),
            approved=True,
        ),
    )
    assert state.phase is RunPhase.SUCCEEDED
    assert commands == ()


def test_generic_interpreter_retains_multiple_human_reviews() -> None:
    definition = _multi_review_definition()
    state = _start_definition(definition)
    state, _ = decide(
        state,
        StepCompleted(
            run_id=state.run_id,
            step_id=StepId("work"),
            agent_run_id=state.current_agent_run_id,
            outcome="success",
            summary="Done",
        ),
    )
    first = HumanReviewCompleted(
        run_id=state.run_id,
        step_id=StepId("first-review"),
        approved=True,
        summary="Proceed",
    )
    state, _ = decide(state, first)
    state, _ = decide(
        state,
        StepCompleted(
            run_id=state.run_id,
            step_id=StepId("verify"),
            agent_run_id=state.current_agent_run_id,
            outcome="success",
            summary="Verified",
        ),
    )
    second = HumanReviewCompleted(
        run_id=state.run_id,
        step_id=StepId("second-review"),
        approved=True,
        summary="Approved",
    )
    state, _ = decide(state, second)

    assert state.phase is RunPhase.SUCCEEDED
    assert state.human_reviews == (first, second)


def test_failure_after_an_approved_intermediate_review_is_reported_as_failed() -> None:
    import asyncio

    definition = _multi_review_definition()
    state = _start_definition(definition)
    state, _ = decide(
        state,
        StepCompleted(
            run_id=state.run_id,
            step_id=StepId("work"),
            agent_run_id=state.current_agent_run_id,
            outcome="success",
            summary="Done",
        ),
    )
    state, _ = decide(
        state,
        HumanReviewCompleted(
            run_id=state.run_id,
            step_id=StepId("first-review"),
            approved=True,
        ),
    )
    state, _ = decide(
        state,
        StepCompleted(
            run_id=state.run_id,
            step_id=StepId("verify"),
            agent_run_id=state.current_agent_run_id,
            outcome="failure",
            summary="Verification failed",
        ),
    )
    store = InMemoryStateStore()
    asyncio.run(store.save(state))
    reader = RunReader(store, WorkflowCatalog.from_definitions((definition,)))
    view = asyncio.run(reader.get(state.run_id))

    assert view is not None
    assert view.terminal_outcome == "failed"
    assert view.human_decision is not None
    assert view.human_decision.approved is True


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            lambda agent: [
                oe.agent_step(
                    id="a",
                    name="A",
                    agent=agent,
                    prompt=oe.template("task"),
                    transitions={"*": oe.goto("b")},
                ),
                oe.agent_step(
                    id="b",
                    name="B",
                    agent=agent,
                    prompt=oe.template("task"),
                    transitions={"*": oe.goto("a")},
                ),
            ],
            "cycles are not supported",
        ),
        (
            lambda agent: [
                oe.agent_step(
                    id="a",
                    name="A",
                    agent=agent,
                    prompt=oe.template("task"),
                    transitions={"*": oe.succeed()},
                ),
                oe.agent_step(
                    id="b",
                    name="B",
                    agent=agent,
                    prompt=oe.template("task"),
                    transitions={"*": oe.succeed()},
                ),
            ],
            "unreachable workflow step",
        ),
    ],
)
def test_v1_rejects_cycles_and_unreachable_steps(steps, message: str) -> None:
    agent = oe.agent(id="worker", instructions="Work")
    with pytest.raises(oe.WorkflowValidationError, match=message):
        oe.workflow(
            id="invalid-v1",
            name="Invalid",
            version="v1",
            steps=steps(agent),
        )


def test_engine_config_resolves_workflows_relative_to_its_own_file(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "engine.toml"
    path.write_text('[workflows]\ndirectory = "../definitions"\n')

    loaded = load_engine_config(path, cwd=tmp_path, environ={})

    assert loaded.workflows_directory == (tmp_path / "definitions").resolve()


def test_loader_names_the_source_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    source = """
import openengine as oe
a = oe.agent(id="a", instructions="work")
workflow = oe.workflow(
    id="same-v1", name="Same", version="v1",
    steps=[oe.agent_step(
        id="one", name="One", agent=a, prompt=oe.template("work"),
        transitions={"*": oe.succeed()},
    )],
)
"""
    (tmp_path / "a.py").write_text(source)
    (tmp_path / "b.py").write_text(source)

    with pytest.raises(WorkflowLoadError, match=r"b\.py.*duplicate workflow id"):
        load_workflow_catalog(tmp_path)


def test_checked_in_definition_is_the_implementation_review_source_of_truth() -> None:
    root = Path(__file__).parents[1]
    loaded = load_workflow_catalog(root / "workflows")
    definition = loaded.require(WorkflowId("implementation-review-v1"))
    implementation, review, human = definition.steps

    assert definition.name == "Implementation review"
    assert definition.version == "v1"
    assert definition.workspace.base_ref == ""
    assert definition.naming_profile is not None
    assert definition.naming_profile.agent_id == AgentId("implementation-agent")
    assert "concise display name" in definition.naming_profile.instructions
    assert "at most eight words" in definition.naming_prompt
    assert isinstance(implementation, AgentStep)
    assert implementation.step_id == StepId("implementation")
    assert implementation.profile.agent_id == AgentId("implementation-agent")
    assert implementation.required_outputs == ("pr_url",)
    assert implementation.editable is True
    assert implementation.workspace_access is WorkspaceAccess.WRITE
    assert isinstance(review, AgentStep)
    assert review.step_id == StepId("review")
    assert review.profile.agent_id == AgentId("review-agent")
    assert review.profile.capabilities == (
        "view_change_request",
        "list_pipeline_status",
        "get_job_logs",
        "add_comment",
    )
    assert review.required_outputs == ("findings",)
    assert review.editable is False
    assert review.workspace_access is WorkspaceAccess.READ
    assert isinstance(human, HumanReviewStep)
    assert human.notification is not None
    assert human.notification.channel == "OpenEngine"
    assert human.notification.public_url == "https://sheas-mac-mini.taileb7fdb.ts.net"


def test_sqlite_round_trips_a_workflow_definition_snapshot() -> None:
    async def scenario() -> None:
        definition = _definition()
        store = SQLiteStateStore(":memory:")
        state = RunState(
            run_id=RunId("run-snapshot"),
            task_id=TaskId("task-snapshot"),
            workflow_id=definition.workflow_id,
            workflow_definition=definition,
            agent_paused=True,
            runner_name="claude",
        )
        await store.save(state)

        assert await store.load(state.run_id) == state

    import asyncio

    asyncio.run(scenario())
