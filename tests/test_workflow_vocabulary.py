"""The vocabulary for describing workflow steps and their completion."""

from dataclasses import FrozenInstanceError, asdict

import pytest

from engine.domain import (
    AgentId,
    AgentRunId,
    Event,
    RunId,
    StepCompleted,
    StepId,
    StepOutput,
    StepSpec,
)


def test_a_step_spec_names_an_agent_and_its_required_outputs() -> None:
    step = StepSpec(
        step_id=StepId("change"),
        agent_id=AgentId("coder"),
        required_outputs=("revision",),
    )

    assert step.step_id == StepId("change")
    assert step.agent_id == AgentId("coder")
    assert step.required_outputs == ("revision",)


def test_step_completion_is_an_event_with_string_outputs() -> None:
    step = StepSpec(step_id=StepId("change"), agent_id=AgentId("coder"))
    event = StepCompleted(
        run_id=RunId("run-1"),
        step_id=step.step_id,
        agent_run_id=AgentRunId("agent-run-1"),
        outcome="success",
        summary="Implemented and pushed the change.",
        outputs=(
            StepOutput(name="revision", value="abc123"),
            StepOutput(name="branch", value="engine/ws-42"),
        ),
    )

    assert isinstance(event, Event)
    assert event.run_id == RunId("run-1")
    assert event.outcome == "success"
    assert event.outputs[0].value == "abc123"
    assert asdict(event) == {
        "run_id": "run-1",
        "step_id": "change",
        "agent_run_id": "agent-run-1",
        "outcome": "success",
        "summary": "Implemented and pushed the change.",
        "outputs": (
            {"name": "revision", "value": "abc123"},
            {"name": "branch", "value": "engine/ws-42"},
        ),
    }


def test_collection_fields_default_to_empty_tuples() -> None:
    step = StepSpec(step_id=StepId("review"), agent_id=AgentId("reviewer"))
    event = StepCompleted(
        run_id=RunId("run-1"),
        step_id=step.step_id,
        agent_run_id=AgentRunId("agent-run-1"),
        outcome="changes_requested",
        summary="Please add a regression test.",
    )

    assert step.required_outputs == ()
    assert event.outputs == ()


@pytest.mark.parametrize(
    "value",
    [
        StepOutput(name="revision", value="abc123"),
        StepSpec(step_id=StepId("change"), agent_id=AgentId("coder")),
        StepCompleted(
            run_id=RunId("run-1"),
            step_id=StepId("change"),
            agent_run_id=AgentRunId("agent-run-1"),
            outcome="ready",
            summary="Ready for the next step.",
        ),
    ],
)
def test_workflow_values_are_frozen_and_slotted(value: object) -> None:
    assert not hasattr(value, "__dict__")
    field_name = next(iter(type(value).__dataclass_fields__))  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        setattr(value, field_name, "not allowed")
