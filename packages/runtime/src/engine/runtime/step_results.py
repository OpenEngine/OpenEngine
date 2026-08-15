"""Workflow result instructions and terminal-turn parsing."""

import json
import re
from typing import Any

from engine.domain import AgentRunId, RunFailed, RunId, StepCompleted, StepOutput, StepSpec
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
        "When the task is complete, call the workflow MCP tool `complete_step` with\n"
        "the fields below. If the task cannot be completed, call `fail_step` with a\n"
        "nonempty `reason`. Call exactly one of these terminal tools.\n\n"
        "If those tools are unavailable, your final response must be exactly one JSON object\n"
        "matching this shape, with no Markdown fence or surrounding prose:\n\n"
        f"{json.dumps(example, indent=2)}\n\n"
        "All output values must be strings."
    )


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

    return step_completed_from_arguments(
        run_id=run_id,
        step=step,
        agent_run_id=agent_run_id,
        arguments=result,
    )


def step_completed_from_arguments(
    *,
    run_id: RunId,
    step: StepSpec,
    agent_run_id: AgentRunId,
    arguments: object,
    mcp_request_id: str | int | None = None,
) -> StepCompleted:
    """Validate terminal tool arguments using the legacy result contract."""
    if not isinstance(arguments, dict):
        raise InvalidStepResultError("step result must be a JSON object")
    if mcp_request_id is not None and not set(arguments).issubset(
        {"outcome", "summary", "outputs"}
    ):
        raise InvalidStepResultError(
            "step result may contain only outcome, summary, and outputs"
        )

    result: dict[object, object] = arguments

    outcome = result.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        raise InvalidStepResultError("step result outcome must be a nonempty string")

    summary = result.get("summary")
    if not isinstance(summary, str):
        raise InvalidStepResultError("step result summary must be a string")

    outputs = result.get("outputs", {})
    if not isinstance(outputs, dict):
        raise InvalidStepResultError("step result outputs must be a JSON object")
    if any(not isinstance(name, str) for name in outputs):
        raise InvalidStepResultError("step result output names must be strings")
    if any(not isinstance(value, str) for value in outputs.values()):
        raise InvalidStepResultError("step result output values must be strings")

    return StepCompleted(
        run_id=run_id,
        step_id=step.step_id,
        agent_run_id=agent_run_id,
        outcome=outcome,
        summary=summary,
        outputs=tuple(
            StepOutput(name=name, value=outputs[name]) for name in sorted(outputs)
        ),
        mcp_request_id=mcp_request_id,
    )


def run_failed_from_arguments(
    *,
    run_id: RunId,
    agent_run_id: AgentRunId,
    arguments: object,
    mcp_request_id: str | int,
) -> RunFailed:
    """Validate a bound `fail_step` call and construct its runtime event."""
    if not isinstance(arguments, dict):
        raise InvalidStepResultError("failure result must be a JSON object")
    if set(arguments) != {"reason"}:
        raise InvalidStepResultError("failure result must contain only reason")
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidStepResultError("failure reason must be a nonempty string")
    return RunFailed(
        run_id=run_id,
        reason=reason,
        agent_run_id=agent_run_id,
        mcp_request_id=mcp_request_id,
    )


__all__ = [
    "InvalidStepResultError",
    "run_failed_from_arguments",
    "step_completed_from_arguments",
    "step_completed_from_turn",
    "step_result_instructions",
]
