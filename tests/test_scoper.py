"""Contract tests for the work-order scoper boundary."""

import inspect

import pytest

from engine.domain import (
    MilestoneId,
    MilestoneScope,
    ScopingPlan,
    ScopingPolicy,
    Supersession,
    WorkOrder,
    WorkOrderId,
    WorkOrderSpec,
    WorkOrderStatus,
)
from engine.scoper import scope


def test_scoping_plan_can_describe_all_supported_changes() -> None:
    milestone_id = MilestoneId("milestone-authentication")
    implementation_id = WorkOrderId("workorder-login")
    browser_validation = WorkOrderSpec(
        milestone_id,
        "Browser validation",
        "Validate login behavior in a browser.",
        ("Capture a screenshot",),
        (implementation_id,),
    )
    security_review = WorkOrderSpec(
        milestone_id,
        "Security review",
        "Review the completed login implementation.",
        dependencies=(implementation_id,),
    )

    plan = ScopingPlan(
        create=(browser_validation, security_review),
        cancel=(WorkOrderId("workorder-obsolete"),),
        supersede=(
            Supersession(WorkOrderId("workorder-broad-review"), (security_review,)),
        ),
        reasons=("Implementation is complete; required validation is missing.",),
    )

    assert plan.create == (browser_validation, security_review)
    assert plan.cancel == (WorkOrderId("workorder-obsolete"),)
    assert plan.supersede[0].replacements == (security_review,)
    assert plan.reasons


def test_scope_exposes_desired_state_inputs_and_is_stubbed() -> None:
    milestone = MilestoneScope(
        MilestoneId("milestone-authentication"),
        requirements=("Implement login", "Validate browser behavior"),
        evidence_requirements=("Capture a screenshot", "Security review"),
    )
    completed_implementation = WorkOrder(
        WorkOrderId("workorder-login"),
        WorkOrderSpec(
            milestone.milestone_id,
            "Implement login",
            "Implement the login flow.",
        ),
        WorkOrderStatus.COMPLETE,
    )

    assert tuple(inspect.signature(scope).parameters) == (
        "workorders",
        "milestones",
        "policy",
    )
    with pytest.raises(NotImplementedError, match="LangGraph"):
        scope(
            workorders=(completed_implementation,),
            milestones=(milestone,),
            policy=ScopingPolicy(),
        )
