"""Golden behavior captured from the pre-DSL implementation-review reducer."""

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

import pytest

from engine.core import decide
from engine.domain import (
    AgentRunCompleted,
    HumanReviewCompleted,
    RunId,
    RunRequested,
    RunState,
    StepCompleted,
    StepOutput,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
    WorkspaceProvisioned,
)
from engine.runtime import load_workflow_catalog, resolve_default_branch


ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.workflow_removal_acceptance
GOLDEN = Path(__file__).parent / "fixtures" / "implementation_review_trace.json"
WORKFLOW_ID = WorkflowId("implementation-review-v1")
TASK_PROMPT = "Fix the queue race and add a regression test."


def _events() -> tuple[object, ...]:
    run_id = RunId("golden-run")
    return (
        RunRequested(
            run_id=run_id,
            task_id=TaskId("task-golden-run"),
            prompt=TASK_PROMPT,
            repository="acme/widgets",
            workflow_id=WORKFLOW_ID,
        ),
        WorkspaceProvisioned(
            run_id=run_id,
            workspace_id=WorkspaceId("workspace-golden-run"),
            root_path="/legacy/worktrees/golden-run",
        ),
        StepCompleted(
            run_id=run_id,
            step_id=StepId("implementation"),
            agent_run_id="golden-run:implementation:run",
            outcome="success",
            summary="Implemented the queue fix with a regression test.",
            outputs=(
                StepOutput(
                    "pr_url", "https://github.com/acme/widgets/pull/n"
                ),
            ),
        ),
        StepCompleted(
            run_id=run_id,
            step_id=StepId("review"),
            agent_run_id="golden-run:review:run",
            outcome="changes_requested",
            summary="One non-blocking naming issue remains.",
            outputs=(StepOutput("findings", "Rename the queue fixture."),),
        ),
        HumanReviewCompleted(
            run_id=run_id,
            step_id=StepId("human-review"),
            approved=True,
            summary="Accepted for the legacy fixture.",
        ),
    )


def _trace_scenarios() -> dict[str, tuple[object, ...]]:
    happy_path = _events()
    run_id = RunId("golden-run")
    return {
        "approved_after_review_changes": happy_path,
        "rejected_after_review_changes": (
            *happy_path[:4],
            HumanReviewCompleted(
                run_id=run_id,
                step_id=StepId("human-review"),
                approved=False,
                summary="Rejected for the legacy fixture.",
            ),
        ),
        "implementation_step_failure": (
            *happy_path[:2],
            StepCompleted(
                run_id=run_id,
                step_id=StepId("implementation"),
                agent_run_id="golden-run:implementation:run",
                outcome="failed",
                summary="Implementation could not be completed.",
            ),
        ),
        "review_agent_failure": (
            *happy_path[:3],
            AgentRunCompleted(
                run_id=run_id,
                agent_run_id="golden-run:review:run",
                succeeded=False,
                summary="Reviewer process exited unexpectedly.",
            ),
        ),
    }


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return str(value) if type(value).__module__ == "engine.domain.ids" else value


def _state(state) -> dict[str, object]:
    current_agent_run_id = (
        None
        if state.phase.value in {"succeeded", "failed"}
        else _json_value(state.current_agent_run_id)
    )
    return {
        "phase": state.phase.value,
        "current_step_id": _json_value(state.current_step_id),
        "current_agent_run_id": current_agent_run_id,
        "agent_runs": _json_value(state.agent_runs),
        "step_results": [
            {
                "step_id": _json_value(result.step_id),
                "outcome": result.outcome,
                "summary": result.summary,
                "outputs": _json_value(result.outputs),
            }
            for result in state.step_results
        ],
        "human_review": _json_value(state.human_review),
        "failure_reason": state.failure_reason,
    }


def _command(command) -> dict[str, object]:
    result: dict[str, object] = {"type": type(command).__name__}
    for name in ("repository", "base_ref", "title", "summary"):
        if hasattr(command, name):
            result[name] = _json_value(getattr(command, name))
    if type(command).__name__ == "StartAgentRun":
        result.update(
            {
                "agent_run_id": _json_value(command.agent_run_id),
                "instance_id": _json_value(command.instance_id),
                "agent_id": _json_value(command.profile.agent_id),
                "capabilities": list(command.profile.capabilities),
                "prompt": command.prompt,
                "workspace_id": _json_value(command.workspace_id),
                "step": {
                    "step_id": _json_value(command.step.step_id),
                    "required_outputs": list(command.step.required_outputs),
                    "editable": command.step.editable,
                },
            }
        )
    if type(command).__name__ == "RequestHumanReview":
        result["step_id"] = _json_value(command.step_id)
    return result


def test_repository_workflow_matches_pre_dsl_golden_event_trace() -> None:
    golden = json.loads(GOLDEN.read_text())
    assert golden["generated_from"] == "d15456f"
    definition = resolve_default_branch(
        load_workflow_catalog(ROOT / "workflows").require(WORKFLOW_ID), "main"
    )
    actual = []
    for name, events in _trace_scenarios().items():
        state = RunState(
            run_id=RunId("golden-run"),
            task_id=TaskId("task-golden-run"),
            workflow_id=WORKFLOW_ID,
        )
        transitions = []
        for event in events:
            state, commands = decide(state, event, definition)
            transitions.append(
                {
                    "event": type(event).__name__,
                    "state": _state(state),
                    "commands": [_command(command) for command in commands],
                }
            )
        actual.append({"name": name, "transitions": transitions})

    assert actual == golden["traces"]
