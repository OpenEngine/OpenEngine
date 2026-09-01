"""Deterministic gates for the model-scored scoper evaluation suite."""

import json
from pathlib import Path

import pytest

from engine.domain import (
    MilestoneId,
    MilestoneScope,
    ScopingPlan,
    Supersession,
    WorkOrder,
    WorkOrderId,
    WorkOrderSpec,
    WorkOrderStatus,
)
from engine.scoper.evals import EvalCase, JudgeScore, load_cases, validate_plan


def _case() -> EvalCase:
    milestone = MilestoneScope(MilestoneId("auth"), requirements=("Login",))
    existing = WorkOrder(
        WorkOrderId("login"),
        WorkOrderSpec(milestone.milestone_id, "Implement login", "Build login."),
        WorkOrderStatus.COMPLETE,
    )
    return EvalCase("existing", "Do not duplicate completed work", (milestone,), (existing,))


def test_valid_plan_has_no_structural_issues() -> None:
    case = _case()
    plan = ScopingPlan(create=(WorkOrderSpec(
        MilestoneId("auth"), "Review login", "Perform a security review.",
        dependencies=(WorkOrderId("login"),),
    ),))

    assert validate_plan(case, plan) == ()


def test_validator_reports_all_hard_failures() -> None:
    case = _case()
    duplicate = WorkOrderSpec(
        MilestoneId("auth"), "Implement login", "Build login",
        dependencies=(WorkOrderId("invented"),),
    )
    plan = ScopingPlan(
        create=(duplicate, duplicate),
        cancel=(WorkOrderId("missing"), WorkOrderId("login")),
        supersede=(Supersession(WorkOrderId("login"), ()),),
    )

    assert {issue.code for issue in validate_plan(case, plan)} == {
        "recreates_existing_work",
        "dangling_dependency",
        "duplicate_proposal",
        "unknown_existing_work",
        "duplicate_change",
    }


def test_judge_score_requires_bounded_structured_output() -> None:
    score = JudgeScore.from_json(json.dumps({
        "chunk_size": 5,
        "reviewability": 4,
        "idempotency": 4,
        "dependencies": 5,
        "coverage": 3,
        "minimality": 3,
        "pass": True,
        "reasons": ["Each work order has one objective."],
    }))

    assert score.mean == 4
    assert score.pass_ is True
    with pytest.raises(ValueError, match="1 to 5"):
        JudgeScore.from_json(json.dumps({
            "chunk_size": 6, "reviewability": 4, "idempotency": 4,
            "dependencies": 5, "coverage": 3, "minimality": 3,
            "pass": True, "reasons": [],
        }))


def test_versioned_dataset_keeps_test_cases_held_out() -> None:
    path = Path(__file__).parent.parent / "evals/scoper/cases.json"
    cases = load_cases(path)

    assert {case["split"] for case in cases} == {"train", "dev", "test"}
    assert all(case["name"] and case["description"] for case in cases)
