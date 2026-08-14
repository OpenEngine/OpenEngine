"""Workflow result instructions and terminal-turn parsing."""

import json
import re
from typing import Any

from engine.domain import (
    AgentRunId,
    RunFailed,
    RunId,
    StepCompleted,
    StepOutput,
    StepSpec,
    ToolCall,
    ToolParameter,
    ToolParameterType,
    ToolSpec,
)
from engine.ports import AgentTurn, FinishReason


class InvalidStepResultError(ValueError):
    """A terminal agent turn does not contain a valid workflow result."""


_JSON_FENCE = re.compile(
    r"```(?:json)?[ \t]*(?:\r?\n)?(?P<content>.*?)(?:\r?\n)?```",
    re.IGNORECASE | re.DOTALL,
)


def _result_content(content: str) -> str:
    """Return plain JSON from a bare response or one Markdown JSON fence."""
    stripped = content.strip()
    fenced = _JSON_FENCE.fullmatch(stripped)
    return fenced.group("content").strip() if fenced is not None else stripped


def step_result_instructions(step: StepSpec) -> str:
    """Return the final-response contract for this step."""
    example = {
        "outcome": "success",
        "summary": "Short human-readable description of the result",
        "outputs": {name: "abc123" for name in step.required_outputs},
    }
    return (
        "When the task is complete, your final response must be exactly one JSON object\n"
        "matching this shape, with no Markdown fence or surrounding prose:\n\n"
        f"{json.dumps(example, indent=2)}\n\n"
        "All output values must be strings."
    )


def complete_step_tool(step: StepSpec) -> ToolSpec:
    """Describe the provider-neutral tool for completing ``step``."""
    outputs = tuple(
        ToolParameter(
            name=name,
            description=f"Value of the required {name!r} step output.",
        )
        for name in step.required_outputs
    )
    return ToolSpec(
        name="complete_step",
        description=f"Complete workflow step {step.step_id!s} successfully.",
        parameters=(
            ToolParameter(
                name="outcome",
                description="The terminal outcome.",
                choices=("success",),
            ),
            ToolParameter(
                name="summary",
                description="A human-readable summary of what was completed.",
            ),
            ToolParameter(
                name="outputs",
                type=ToolParameterType.OBJECT,
                description="The step's declared outputs.",
                properties=outputs,
            ),
        ),
    )


def fail_step_tool(step: StepSpec) -> ToolSpec:
    """Describe the provider-neutral tool for failing ``step``."""
    return ToolSpec(
        name="fail_step",
        description=f"Fail workflow step {step.step_id!s}.",
        parameters=(
            ToolParameter(
                name="summary",
                description="Why the step could not be completed.",
            ),
        ),
    )


def _arguments_from_tool_call(call: ToolCall, expected_name: str) -> dict[str, Any]:
    if call.name != expected_name:
        raise InvalidStepResultError(
            f"expected {expected_name!r} tool call, got {call.name!r}"
        )
    try:
        arguments: Any = json.loads(call.arguments)
    except (json.JSONDecodeError, TypeError) as error:
        raise InvalidStepResultError(
            f"{expected_name} arguments are not valid JSON"
        ) from error
    if not isinstance(arguments, dict):
        raise InvalidStepResultError(f"{expected_name} arguments must be a JSON object")
    return arguments


def _validate_outputs(step: StepSpec, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise InvalidStepResultError("step result outputs must be a JSON object")
    if any(not isinstance(name, str) for name in value):
        raise InvalidStepResultError("step result output names must be strings")

    expected = set(step.required_outputs)
    actual = set(value)
    missing = sorted(expected - actual)
    if missing:
        raise InvalidStepResultError(
            f"step result is missing required outputs: {', '.join(missing)}"
        )
    extra = sorted(actual - expected)
    if extra:
        raise InvalidStepResultError(
            f"step result has undeclared outputs: {', '.join(extra)}"
        )
    if any(not isinstance(output, str) for output in value.values()):
        raise InvalidStepResultError("step result output values must be strings")
    return value


def _step_completed(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    result: dict[str, Any],
    require_success: bool,
    require_declared_outputs: bool,
) -> StepCompleted:
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        raise InvalidStepResultError("step result outcome must be a nonempty string")
    if require_success and outcome != "success":
        raise InvalidStepResultError("complete_step outcome must be 'success'")

    summary = result.get("summary")
    if not isinstance(summary, str):
        raise InvalidStepResultError("step result summary must be a string")

    outputs_value = result.get("outputs", {})
    if require_declared_outputs:
        outputs = _validate_outputs(step, outputs_value)
    else:
        if not isinstance(outputs_value, dict):
            raise InvalidStepResultError("step result outputs must be a JSON object")
        if any(not isinstance(name, str) for name in outputs_value):
            raise InvalidStepResultError("step result output names must be strings")
        if any(not isinstance(output, str) for output in outputs_value.values()):
            raise InvalidStepResultError("step result output values must be strings")
        outputs = outputs_value
    return StepCompleted(
        run_id=run_id,
        step_id=step.step_id,
        agent_run_id=agent_run_id,
        outcome=outcome,
        summary=summary,
        outputs=tuple(
            StepOutput(name=name, value=outputs[name]) for name in sorted(outputs)
        ),
    )


def step_completed_from_tool_call(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    call: ToolCall,
) -> StepCompleted:
    """Validate a ``complete_step`` call and return its completion event."""
    result = _arguments_from_tool_call(call, "complete_step")
    if set(result) != {"outcome", "summary", "outputs"}:
        raise InvalidStepResultError(
            "complete_step arguments must be exactly outcome, summary, and outputs"
        )
    return _step_completed(
        run_id=run_id,
        step=step,
        agent_run_id=agent_run_id,
        result=result,
        require_success=True,
        require_declared_outputs=True,
    )


def run_failed_from_tool_call(*, run_id: RunId, call: ToolCall) -> RunFailed:
    """Validate a ``fail_step`` call and return its run failure event."""
    result = _arguments_from_tool_call(call, "fail_step")
    if set(result) != {"summary"}:
        raise InvalidStepResultError("fail_step requires exactly one summary argument")
    summary = result["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidStepResultError("fail_step summary must be a nonempty string")
    return RunFailed(run_id=run_id, reason=summary)


def step_result_from_tool_call(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    call: ToolCall,
) -> StepCompleted | RunFailed:
    """Parse either terminal workflow tool call into its domain event."""
    if call.name == "complete_step":
        return step_completed_from_tool_call(
            run_id=run_id,
            step=step,
            agent_run_id=agent_run_id,
            call=call,
        )
    if call.name == "fail_step":
        return run_failed_from_tool_call(run_id=run_id, call=call)
    raise InvalidStepResultError(f"unknown step result tool: {call.name!r}")


def step_completed_from_turn(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    turn: AgentTurn,
) -> StepCompleted:
    """Parse a terminal agent turn into a workflow completion event."""
    if turn.finish_reason is not FinishReason.STOP:
        raise InvalidStepResultError(
            f"step result requires a STOP turn, got {turn.finish_reason.value!r}"
        )

    try:
        result: Any = json.loads(_result_content(turn.message.content))
    except (json.JSONDecodeError, TypeError, AttributeError) as error:
        raise InvalidStepResultError("step result is not valid JSON") from error

    if not isinstance(result, dict):
        raise InvalidStepResultError("step result must be a JSON object")

    return _step_completed(
        run_id=run_id,
        step=step,
        agent_run_id=agent_run_id,
        result=result,
        require_success=False,
        require_declared_outputs=False,
    )


__all__ = [
    "InvalidStepResultError",
    "complete_step_tool",
    "fail_step_tool",
    "run_failed_from_tool_call",
    "step_completed_from_turn",
    "step_completed_from_tool_call",
    "step_result_instructions",
    "step_result_from_tool_call",
]
