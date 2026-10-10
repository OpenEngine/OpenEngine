"""Contract tests for the work-order scoper boundary."""

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest
from langgraph_acp import ACPAgentRegistry, StdioACPProvider

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
from engine.scoper import Scoper, _plan, scope


@pytest.mark.parametrize("supersede", [False, True])
@pytest.mark.parametrize("source_ref", [
    "github_issue", ["kind", "ref"], 1, True, "", [], 0, False, {},
    {"kind": "github_issue"}, {"ref": "https://github.com/org/repo/issues/12"},
    {"kind": "github_issue", "repository": "org/repo"},
    {"repository": "org/repo", "number": 12},
    {"kind": "github_issue", "number": 12},
    {"kind": "github_issue", "repository": "org/repo", "number": 12, "extra": "unexpected"},
])
def test_scoper_rejects_malformed_source_refs(source_ref, supersede) -> None:
    ticket = {
        "milestone_id": "m", "name": "API", "objective": "Ship API",
        "source_ref": source_ref,
    }
    response = (
        {"supersede": [{"workorder_id": "old", "replacements": [ticket]}]}
        if supersede else {"create": [ticket]}
    )
    with pytest.raises(ValueError, match="source_ref"):
        _plan(json.dumps(response))


@pytest.mark.parametrize("fields", [{}, {"source_ref": None}])
def test_scoper_allows_absent_source_ref(fields) -> None:
    plan = _plan(json.dumps({"create": [{
        "milestone_id": "m", "name": "API", "objective": "Ship API", **fields,
    }]}))
    assert plan.create[0].source_ref is None


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


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_scope_returns_new_scope_from_mocked_agent(
    agent: str, tmp_path: Path
) -> None:
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

    in_progress_validation = WorkOrder(
        WorkOrderId("workorder-browser-validation"),
        WorkOrderSpec(
            milestone.milestone_id,
            "Browser validation",
            "Validate the login flow in a browser.",
            ("Capture a screenshot",),
            (completed_implementation.workorder_id,),
        ),
        WorkOrderStatus.IN_PROGRESS,
    )

    response = json.dumps({
        "create": [{
            "milestone_id": "milestone-authentication",
            "name": "Security review",
            "objective": "Review the completed login implementation.",
            "evidence_requirements": ["Security review"],
            "dependencies": ["workorder-login"],
        }],
        "cancel": [],
        "supersede": [],
        "reasons": [
            "Implementation is complete and browser validation is underway."
        ],
    })
    log = tmp_path / f"{agent}-acp.jsonl"
    fake_agent = Path(__file__).parent.parent / "langgraph-acp/tests/fake_agent.py"
    registry = ACPAgentRegistry([
        StdioACPProvider(
            name=agent,
            command=(sys.executable, str(fake_agent)),
            env={"FAKE_AGENT_LOG": str(log), "FAKE_AGENT_RESPONSE": response},
        )
    ])

    assert tuple(inspect.signature(scope).parameters) == (
        "workorders",
        "milestones",
        "policy",
    )
    plan = asyncio.run(
        Scoper(agent=agent, registry=registry).scope(
            workorders=(completed_implementation, in_progress_validation),
            milestones=(milestone,),
            policy=ScopingPolicy(rules=("Prefer independently reviewable work",)),
        )
    )

    messages = [json.loads(line) for line in log.read_text().splitlines()]
    prompt = next(
        message["params"]["prompt"][0]["text"]
        for message in messages
        if message.get("method") == "session/prompt"
    )
    inputs = json.loads(prompt.split("Inputs:\n", 1)[1])
    assert inputs["workorders"][0]["status"] == "complete"
    assert inputs["workorders"][1]["status"] == "in_progress"

    assert plan == ScopingPlan(
        create=(WorkOrderSpec(
            milestone.milestone_id,
            "Security review",
            "Review the completed login implementation.",
            ("Security review",),
            (completed_implementation.workorder_id,),
        ),),
        reasons=("Implementation is complete and browser validation is underway.",),
    )


@pytest.mark.parametrize("supersede", [False, True])
@pytest.mark.parametrize("kind", ["github_issue", "github_milestone"])
@pytest.mark.parametrize("input_location", ["milestone", "workorder"])
@pytest.mark.parametrize("change", [
    {}, {"repository": "other/repo"}, {"number": 13},
    {"kind": "github_milestone"}, {"milestone_id": "other"},
])
def test_scoper_checks_source_against_inputs(supersede, kind, input_location, change):
    from langgraph_acp import ACPResult
    from engine.domain import TicketSourceRef

    source = TicketSourceRef(kind, "org/repo", 12)
    raw_source = {"kind": kind, "repository": "org/repo", "number": 12}
    returned_source = {**raw_source, **{k: v for k, v in change.items() if k != "milestone_id"}}
    ticket = {
        "milestone_id": change.get("milestone_id", "m"),
        "name": "API", "objective": "Ship API", "source_ref": returned_source,
    }
    response = (
        {"supersede": [{"workorder_id": "old", "replacements": [ticket]}]}
        if supersede else {"create": [ticket]}
    )
    milestone = MilestoneScope(
        MilestoneId("m"), source_refs=(source,) if input_location == "milestone" else (),
    )
    workorders = (
        WorkOrder(WorkOrderId("old"), WorkOrderSpec(
            MilestoneId("m"), "API", "Ship API", source_ref=source,
        )),
    ) if input_location == "workorder" else ()

    async def node(prompt):
        inputs = json.loads(prompt.split("Inputs:\n", 1)[1])
        provided = (
            inputs["milestones"][0]["source_refs"][0]
            if input_location == "milestone"
            else inputs["workorders"][0]["spec"]["source_ref"]
        )
        assert provided == raw_source
        return ACPResult(message=json.dumps(response))

    async def run():
        return await Scoper(node=node).scope(
            workorders=workorders, milestones=(milestone,), policy=ScopingPolicy(),
        )

    if returned_source != raw_source or ticket["milestone_id"] != "m":
        with pytest.raises(ValueError, match="source_ref was not supplied"):
            asyncio.run(run())
    else:
        plan = asyncio.run(run())
        result = plan.supersede[0].replacements[0] if supersede else plan.create[0]
        assert result.source_ref == source


def test_scoper_rejects_source_without_structured_input():
    response = json.dumps({"create": [{
        "milestone_id": "m", "name": "API", "objective": "Ship API",
        "source_ref": {"kind": "github_issue", "repository": "org/repo", "number": 12},
    }]})
    with pytest.raises(ValueError, match="source_ref was not supplied"):
        _plan(response, milestones=(MilestoneScope(
            MilestoneId("m"), requirements=("Implement https://github.com/org/repo/issues/12",),
        ),))
