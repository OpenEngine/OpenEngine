"""Workflow terminal-tool instructions and argument validation."""

import json

import pytest

from engine.domain import (
    AgentId,
    AgentRunId,
    Message,
    RunFailed,
    RunId,
    StepCompleted,
    StepId,
    StepOutput,
    StepSpec,
    ToolCall,
    ToolParameterType,
)
from engine.ports import AgentTurn
from engine.runtime import (
    InvalidStepResultError,
    complete_step_tool,
    fail_step_tool,
    requests_clarification_or_escalation,
    run_failed_from_tool_call,
    step_completed_from_tool_call,
    step_result_instructions,
    step_result_from_tool_call,
)
from engine.runtime.step_results import (
    latest_turn_requests_clarification_or_escalation,
)


RUN_ID = RunId("run-1")
AGENT_RUN_ID = AgentRunId("agent-run-1")
STEP = StepSpec(
    step_id=StepId("change"),
    agent_id=AgentId("coder"),
    required_outputs=("revision",),
)


def test_instructions_require_a_valid_tool_call_without_a_json_fallback() -> None:
    instructions = step_result_instructions(STEP)

    assert "`complete_step`" in instructions
    assert "`fail_step`" in instructions
    assert "clarification or escalation tool" in instructions
    assert "revision" in instructions
    assert "JSON" not in instructions


@pytest.mark.parametrize(
    "tool_name",
    [
        "AskUserQuestion",
        "request_user_input",
        "functions.request_clarification",
        "mcp__workflow__request_human_review",
        "escalate_to_human",
    ],
)
def test_clarification_and_escalation_calls_are_valid_pauses(tool_name: str) -> None:
    turn = AgentTurn(
        Message.assistant("Waiting for an answer."),
        steps=(
            Message.assistant(
                tool_calls=(ToolCall("call-1", tool_name, "{}"),)
            ),
        ),
    )

    assert requests_clarification_or_escalation(turn)


def test_ordinary_tool_calls_are_not_valid_pauses() -> None:
    turn = AgentTurn(
        Message.assistant("Done."),
        steps=(
            Message.assistant(tool_calls=(ToolCall("call-1", "Edit", "{}"),)),
        ),
    )

    assert not requests_clarification_or_escalation(turn)


def test_only_the_latest_conversation_turn_can_still_be_waiting() -> None:
    clarification = Message.assistant(
        tool_calls=(ToolCall("call-1", "request_user_input", "{}"),)
    )

    assert latest_turn_requests_clarification_or_escalation(
        (Message.user("Start"), clarification, Message.assistant("Waiting."))
    )
    assert not latest_turn_requests_clarification_or_escalation(
        (
            Message.user("Start"),
            clarification,
            Message.assistant("Waiting."),
            Message.user("Here is the answer."),
        )
    )


def test_complete_step_tool_describes_required_arguments_and_outputs() -> None:
    tool = complete_step_tool(STEP)

    assert tool.name == "complete_step"
    assert tool.required_parameters == ("outcome", "summary", "outputs")
    outcome, _, outputs = tool.parameters
    assert outcome.choices == ("success",)
    assert outputs.type is ToolParameterType.OBJECT
    assert tuple(property.name for property in outputs.properties) == ("revision",)
    assert outputs.required_properties == ("revision",)


def test_fail_step_tool_requires_a_summary() -> None:
    tool = fail_step_tool(STEP)

    assert tool.name == "fail_step"
    assert tool.required_parameters == ("summary",)


def test_valid_complete_step_call_produces_step_completed() -> None:
    call = ToolCall(
        call_id="call-1",
        name="complete_step",
        arguments=json.dumps(
            {
                "outcome": "success",
                "summary": "Implemented the change.",
                "outputs": {"revision": "abc123"},
            }
        ),
    )

    completed = step_completed_from_tool_call(
        run_id=RUN_ID,
        step=STEP,
        agent_run_id=AGENT_RUN_ID,
        call=call,
    )

    assert completed == StepCompleted(
        run_id=RUN_ID,
        step_id=STEP.step_id,
        agent_run_id=AGENT_RUN_ID,
        outcome="success",
        summary="Implemented the change.",
        outputs=(StepOutput(name="revision", value="abc123"),),
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"outcome": "success", "summary": "Done.", "outputs": {}},
        {
            "outcome": "success",
            "summary": "Done.",
            "outputs": {"revision": "abc123", "extra": "value"},
        },
        {
            "outcome": "success",
            "summary": "Done.",
            "outputs": {"revision": 123},
        },
    ],
)
def test_complete_step_call_rejects_malformed_outputs(
    arguments: dict[str, object],
) -> None:
    call = ToolCall("call-1", "complete_step", json.dumps(arguments))

    with pytest.raises(InvalidStepResultError):
        step_completed_from_tool_call(
            run_id=RUN_ID,
            step=STEP,
            agent_run_id=AGENT_RUN_ID,
            call=call,
        )


@pytest.mark.parametrize("summary", ["", "   ", None, 123])
def test_fail_step_call_rejects_an_invalid_summary(summary: object) -> None:
    call = ToolCall("call-1", "fail_step", json.dumps({"summary": summary}))

    with pytest.raises(InvalidStepResultError):
        run_failed_from_tool_call(run_id=RUN_ID, call=call)


def test_fail_step_call_produces_run_failed() -> None:
    call = ToolCall(
        "call-1",
        "fail_step",
        json.dumps({"summary": "The dependency is unavailable."}),
    )

    result = step_result_from_tool_call(
        run_id=RUN_ID,
        step=STEP,
        agent_run_id=AGENT_RUN_ID,
        call=call,
    )

    assert result == RunFailed(
        run_id=RUN_ID,
        reason="The dependency is unavailable.",
    )
