"""Compile a DSPy scoper program without exposing the held-out test split."""

from __future__ import annotations

import json
from pathlib import Path

import dspy

from engine.scoper import INSTRUCTIONS
from engine.scoper.evals import JUDGE_RUBRIC, load_cases

ROOT = Path(__file__).resolve().parents[2]


class ScopeWork(dspy.Signature):
    """Produce a small, idempotent work-order plan with explicit dependencies."""

    inputs: str = dspy.InputField(desc="Milestones, existing work, and policy as JSON")
    plan: str = dspy.OutputField(desc=INSTRUCTIONS)


def metric(example: dspy.Example, prediction: dspy.Prediction, trace=None) -> float:
    """Use a structured judge for optimization; invalid JSON receives zero."""

    del trace
    try:
        json.loads(prediction.plan)
    except (json.JSONDecodeError, TypeError):
        return 0.0
    judge = dspy.Predict("case, plan, rubric -> score: int")
    result = judge(case=example.inputs, plan=prediction.plan, rubric=JUDGE_RUBRIC)
    return max(0.0, min(1.0, int(result.score) / 5))


def main() -> None:
    cases = load_cases(ROOT / "evals/scoper/cases.json")
    train = [
        dspy.Example(inputs=json.dumps(case, separators=(",", ":"))).with_inputs("inputs")
        for case in cases if case["split"] in {"train", "dev"}
    ]
    program = dspy.Predict(ScopeWork)
    compiled = dspy.MIPROv2(metric=metric, auto="light").compile(program, trainset=train)
    output = ROOT / "evals/scoper/compiled.json"
    compiled.save(str(output))
    print(f"saved optimized DSPy program to {output}")


if __name__ == "__main__":
    main()
