"""Generate authentic pre-DSL SQLite and event-trace acceptance fixtures.

Run this script with ``engine`` imports resolving to commit ``d15456f``. It is
not imported by pytest; generation is a deliberate maintenance operation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.core import decide
from engine.domain import (
    AgentInstanceId,
    AgentRun,
    AgentRunCompleted,
    AgentRunStatus,
    ConversationId,
    HumanReviewCompleted,
    Message,
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

SOURCE_COMMIT = "d15456f"
WORKFLOW_ID = WorkflowId("implementation-review-v1")
TASK_PROMPT = "Fix the queue race and add a regression test."
REPOSITORY = "acme/widgets"


def _events(run_id: RunId) -> tuple[object, ...]:
    return (
        RunRequested(
            run_id=run_id,
            task_id=TaskId(f"task-{run_id}"),
            prompt=TASK_PROMPT,
            repository=REPOSITORY,
            workflow_id=WORKFLOW_ID,
        ),
        WorkspaceProvisioned(
            run_id=run_id,
            workspace_id=WorkspaceId(f"workspace-{run_id}"),
            root_path=f"/legacy/worktrees/{run_id}",
        ),
        StepCompleted(
            run_id=run_id,
            step_id=StepId("implementation"),
            agent_run_id=f"{run_id}:implementation:run",
            outcome="success",
            summary="Implemented the queue fix with a regression test.",
            outputs=(
                StepOutput(
                    "pr_url", f"https://github.com/acme/widgets/pull/{run_id[-1]}"
                ),
            ),
        ),
        StepCompleted(
            run_id=run_id,
            step_id=StepId("review"),
            agent_run_id=f"{run_id}:review:run",
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


def _trace_scenarios(run_id: RunId) -> dict[str, tuple[object, ...]]:
    happy_path = _events(run_id)
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
                agent_run_id=f"{run_id}:implementation:run",
                outcome="failed",
                summary="Implementation could not be completed.",
            ),
        ),
        "review_agent_failure": (
            *happy_path[:3],
            AgentRunCompleted(
                run_id=run_id,
                agent_run_id=f"{run_id}:review:run",
                succeeded=False,
                summary="Reviewer process exited unexpectedly.",
            ),
        ),
    }


async def _record_agent_command(store, state, command) -> None:
    if type(command).__name__ != "StartAgentRun":
        return
    step_id = command.step.step_id
    await store.create_instance(
        command.profile.agent_id,
        task_id=state.task_id,
        workspace_id=command.workspace_id,
        runner="legacy-runner",
        instance_id=command.instance_id,
        conversation_id=ConversationId(f"conversation:{state.run_id}:{step_id}"),
        workflow_run_id=state.run_id,
        workflow_step_id=step_id,
    )
    await store.append_messages(command.instance_id, (Message.user(command.prompt),))
    await store.record_agent_run(
        AgentRun(
            agent_run_id=command.agent_run_id,
            instance_id=command.instance_id,
            status=AgentRunStatus.RUNNING,
            runner="legacy-runner",
        )
    )


async def _complete_agent_run(store, event) -> None:
    if not isinstance(event, StepCompleted):
        return
    await store.record_agent_run(
        AgentRun(
            agent_run_id=event.agent_run_id,
            instance_id=AgentInstanceId(
                f"{event.run_id}:{event.step_id}:instance"
            ),
            status=AgentRunStatus.SUCCEEDED,
            summary=event.summary,
            runner="legacy-runner",
        )
    )


async def _build_database(path: Path) -> None:
    store = SQLiteStateStore(path)
    targets = {
        RunId("legacy-implementation-running"): 2,
        RunId("legacy-review-running"): 3,
        RunId("legacy-awaiting-human"): 4,
        RunId("legacy-completed-approved"): 5,
    }
    try:
        for run_id, event_count in targets.items():
            state = RunState(
                run_id=run_id,
                task_id=TaskId(f"task-{run_id}"),
                workflow_id=WORKFLOW_ID,
            )
            for event in _events(run_id)[:event_count]:
                state, commands = decide(state, event)
                await store.append_events(run_id, (event,))
                await _complete_agent_run(store, event)
                for command in commands:
                    await _record_agent_command(store, state, command)
            await store.save(state)
    finally:
        store.close()


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


def _normalized_state(state) -> dict[str, object]:
    phase = {
        "implementing": "running_agent",
        "reviewing": "running_agent",
    }.get(state.phase.value, state.phase.value)
    current_agent_run_id = (
        None
        if phase in {"succeeded", "failed"}
        else _json_value(state.current_agent_run_id)
    )
    return {
        "phase": phase,
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


def _normalized_command(command) -> dict[str, object]:
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


def _build_trace() -> dict[str, object]:
    run_id = RunId("golden-run")
    traces = []
    for name, events in _trace_scenarios(run_id).items():
        state = RunState(
            run_id=run_id,
            task_id=TaskId("task-golden-run"),
            workflow_id=WORKFLOW_ID,
        )
        transitions = []
        for event in events:
            state, commands = decide(state, event)
            transitions.append(
                {
                    "event": type(event).__name__,
                    "state": _normalized_state(state),
                    "commands": [
                        _normalized_command(command) for command in commands
                    ],
                }
            )
        traces.append({"name": name, "transitions": transitions})
    return {
        "generated_from": SOURCE_COMMIT,
        "workflow_id": str(WORKFLOW_ID),
        "description": "Representative success, rejection, and failure paths.",
        "traces": traces,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    database = args.output / "pre_workflow_definitions.sqlite3"
    golden = args.output / "implementation_review_trace.json"
    existing = [path for path in (database, golden) if path.exists()]
    if existing and not args.force:
        parser.error("refusing to overwrite: " + ", ".join(map(str, existing)))
    for path in existing:
        path.unlink()
    asyncio.run(_build_database(database))
    golden.write_text(json.dumps(_build_trace(), indent=2) + "\n")


if __name__ == "__main__":
    main()
