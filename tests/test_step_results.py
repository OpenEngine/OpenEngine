"""Workflow result contracts and terminal-turn parsing."""

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
from engine.ports import AgentTurn, FinishReason
from engine.runtime import (
    InvalidStepResultError,
    complete_step_tool,
    fail_step_tool,
    run_failed_from_tool_call,
    step_completed_from_tool_call,
    step_completed_from_turn,
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


def parse(content: str, **turn_kwargs: object) -> StepCompleted:
    return step_completed_from_turn(
        run_id=RUN_ID,
        step=STEP,
        agent_run_id=AGENT_RUN_ID,
        turn=AgentTurn(Message.assistant(content), **turn_kwargs),
    )


def test_instructions_describe_the_expected_schema() -> None:
    instructions = step_result_instructions(STEP)

    assert "exactly one JSON object" in instructions
    assert '"outcome": "success"' in instructions
    assert '"summary":' in instructions
    assert '"outputs": {' in instructions
    assert '"revision": "abc123"' in instructions
    assert "no Markdown fence or surrounding prose" in instructions
    assert "All output values must be strings." in instructions


def test_valid_json_produces_step_completed() -> None:
    completed = parse(
        json.dumps(
            {
                "outcome": "success",
                "summary": "Implemented the change.",
                "outputs": {"revision": "abc123"},
            }
        )
    )

    assert completed == StepCompleted(
        run_id=RUN_ID,
        step_id=STEP.step_id,
        agent_run_id=AGENT_RUN_ID,
        outcome="success",
        summary="Implemented the change.",
        outputs=(StepOutput(name="revision", value="abc123"),),
    )


def test_omitted_outputs_become_an_empty_tuple() -> None:
    assert parse('{"outcome": "success", "summary": "Done."}').outputs == ()


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"outcome": "success", "summary": "Done.", "outputs": {}}\n```',
        '```json {"outcome": "success", "summary": "Done.", "outputs": {}} ```',
        '```\n{"outcome": "success", "summary": "Done.", "outputs": {}}\n```',
    ],
)
def test_single_markdown_fence_is_accepted(content: str) -> None:
    completed = parse(content)

    assert completed.outcome == "success"
    assert completed.summary == "Done."
    assert completed.outputs == ()


def test_outputs_are_normalized_deterministically() -> None:
    completed = parse(
        '{"outcome": "success", "summary": "Done.", '
        '"outputs": {"zeta": "last", "alpha": "first"}}'
    )

    assert completed.outputs == (
        StepOutput(name="alpha", value="first"),
        StepOutput(name="zeta", value="last"),
    )


def test_open_custom_outcomes_are_accepted() -> None:
    assert parse('{"outcome": "changes_requested", "summary": "Revise it."}').outcome == (
        "changes_requested"
    )


@pytest.mark.parametrize(
    "content",
    [
        "ordinary prose",
        '{"outcome": "success", "summary": "Done."}\nFinished.',
        'Result:\n```json\n{"outcome": "success", "summary": "Done."}\n```',
        '```json\n{"outcome": "success", "summary": "Done."}\n```\nFinished.',
    ],
)
def test_non_json_or_json_with_surrounding_commentary_is_rejected(content: str) -> None:
    with pytest.raises(InvalidStepResultError):
        parse(content)


@pytest.mark.parametrize("value", [1, True, None, ["abc123"]])
def test_non_string_output_values_are_rejected(value: object) -> None:
    content = json.dumps(
        {"outcome": "success", "summary": "Done.", "outputs": {"revision": value}}
    )

    with pytest.raises(InvalidStepResultError):
        parse(content)


@pytest.mark.parametrize(
    "result",
    [
        {"summary": "Done."},
        {"outcome": "", "summary": "Done."},
        {"outcome": "success"},
        {"outcome": "success", "summary": None},
    ],
)
def test_missing_or_invalid_outcome_or_summary_is_rejected(
    result: dict[str, object],
) -> None:
    with pytest.raises(InvalidStepResultError):
        parse(json.dumps(result))


@pytest.mark.parametrize(
    "finish_reason",
    [reason for reason in FinishReason if reason is not FinishReason.STOP],
)
def test_non_stop_finish_reasons_are_rejected(finish_reason: FinishReason) -> None:
    with pytest.raises(InvalidStepResultError):
        parse(
            '{"outcome": "success", "summary": "Done."}',
            finish_reason=finish_reason,
        )


def test_intermediate_turn_steps_are_ignored() -> None:
    completed = parse(
        '{"outcome": "success", "summary": "Done."}',
        steps=(
            Message.assistant("ordinary narration"),
            Message.assistant('{"not": "the result"}'),
        ),
    )

    assert completed.outcome == "success"
    assert completed.summary == "Done."


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
