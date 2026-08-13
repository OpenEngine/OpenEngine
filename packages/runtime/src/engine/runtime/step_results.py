"""Workflow result instructions and terminal-turn parsing."""

import json
from typing import Any

from engine.domain import AgentRunId, RunId, StepCompleted, StepOutput, StepSpec
from engine.ports import AgentTurn, FinishReason


class InvalidStepResultError(ValueError):
    """A terminal agent turn does not contain a valid workflow result."""


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
        result: Any = json.loads(turn.message.content)
    except (json.JSONDecodeError, TypeError) as error:
        raise InvalidStepResultError("step result is not valid JSON") from error

    if not isinstance(result, dict):
        raise InvalidStepResultError("step result must be a JSON object")

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
    )


__all__ = [
    "InvalidStepResultError",
    "step_completed_from_turn",
    "step_result_instructions",
]
