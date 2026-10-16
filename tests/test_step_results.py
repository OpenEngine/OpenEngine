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
    INVALID_COMPLETION_ERROR,
    InvalidStepResultError,
    complete_step_tool,
    fail_step_tool,
    requests_clarification_or_escalation,
    run_failed_from_tool_call,
    step_completed_from_tool_call,
    step_result_instructions,
    step_result_from_tool_call,
)

RUN_ID = RunId("run-1")
AGENT_RUN_ID = AgentRunId("agent-run-1")
STEP = StepSpec(
    step_id=StepId("change"),
    agent_id=AgentId("coder"),
    required_outputs=("revision",),
)


@pytest.mark.parametrize("status_updates", [False, True])
def test_instructions_require_a_valid_tool_call_without_a_json_fallback(
    status_updates: bool,
) -> None:
    instructions = step_result_instructions(STEP, status_updates=status_updates)

    assert "`complete_step`" in instructions
    assert "`clarify`" in instructions
    assert "`fail_step`" in instructions
    assert (
        "If you are only answering a human's question and did not change the "
        "implementation, call `clarify` after answering"
    ) in instructions
    assert "revision" in instructions
    assert "JSON" not in instructions


def test_unattended_instructions_require_independent_decisions() -> None:
    instructions = step_result_instructions(STEP)

    assert (
        "Nobody is watching this step. When you hit a decision, make the call "
        "you think is best, say what you chose and why in your summary, and finish "
        "with `complete_step`. Don't stop to ask."
    ) in instructions
    assert "clarification or escalation tool" not in instructions
    assert "`update_status`" not in instructions


def test_watched_instructions_allow_questions_and_require_status_updates() -> None:
    instructions = step_result_instructions(STEP, status_updates=True)

    assert (
        "If you need information or a human decision before continuing, use an "
        "available clarification or escalation tool other than `clarify`."
    ) in instructions
    assert "`clarify` only finishes answering a person's question; it does not ask one." in instructions
    assert "`update_status`" in instructions
    assert "Nobody is watching" not in instructions
    assert "Don't stop to ask" not in instructions


def test_invalid_completion_error_explains_failure_and_terminal_tools() -> None:
    assert INVALID_COMPLETION_ERROR == (
        "The step did not report a valid terminal result. "
        "Call `complete_step` with the declared outputs, or call `fail_step` "
        "if the step cannot be completed."
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "AskUserQuestion",
        "mcp__workflow__clarify",
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
