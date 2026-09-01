"""Work-order scoping backed by an ACP-compatible planning agent."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from engine.domain import (
    MilestoneId,
    MilestoneScope,
    ScopingPlan,
    ScopingPolicy,
    Supersession,
    WorkOrder,
    WorkOrderId,
    WorkOrderSpec,
)
from langgraph_acp import ACPNode, ACPResult
from langgraph_acp.agent import ACPAgentRegistry

ScopingNode = Callable[[str], Awaitable[ACPResult]]

_INSTRUCTIONS = """You are a work-order scoper. Compare the desired milestones with
the existing work orders. Completed and in-progress work must be taken into account;
do not recreate work they already cover. Return only one JSON object with these keys:
create (work-order specs), cancel (work-order ids), supersede (objects containing a
workorder_id and replacement specs), and reasons (strings). A work-order spec has
milestone_id, name, objective, evidence_requirements, and dependencies. Every list
may be empty. Do not wrap the JSON in Markdown.

Inputs:
"""


def _prompt(
    workorders: Sequence[WorkOrder],
    milestones: Sequence[MilestoneScope],
    policy: ScopingPolicy,
) -> str:
    payload = {
        "milestones": [asdict(item) for item in milestones],
        "workorders": [asdict(item) for item in workorders],
        "policy": asdict(policy),
    }
    return _INSTRUCTIONS + json.dumps(
        payload,
        default=lambda value: value.value if isinstance(value, Enum) else str(value),
        separators=(",", ":"),
    )


def _strings(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"scoper response field {field!r} must be a list of strings")
    return tuple(value)


def _spec(value: object) -> WorkOrderSpec:
    if not isinstance(value, Mapping):
        raise ValueError("scoper response work-order specs must be objects")
    try:
        milestone_id = value["milestone_id"]
        name = value["name"]
        objective = value["objective"]
    except KeyError as error:
        raise ValueError(f"scoper response spec is missing {error.args[0]!r}") from error
    if not all(isinstance(item, str) for item in (milestone_id, name, objective)):
        raise ValueError(
            "scoper response spec identifiers and descriptions must be strings"
        )
    return WorkOrderSpec(
        milestone_id=MilestoneId(milestone_id),
        name=name,
        objective=objective,
        evidence_requirements=_strings(
            value.get("evidence_requirements", []), field="evidence_requirements"
        ),
        dependencies=tuple(
            WorkOrderId(item)
            for item in _strings(value.get("dependencies", []), field="dependencies")
        ),
    )


def _plan(message: str) -> ScopingPlan:
    try:
        value: Any = json.loads(message)
    except json.JSONDecodeError as error:
        raise ValueError("scoper agent did not return valid JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("scoper agent response must be a JSON object")
    create, supersede = value.get("create", []), value.get("supersede", [])
    if not isinstance(create, list) or not isinstance(supersede, list):
        raise ValueError("scoper response create and supersede fields must be lists")
    replacements: list[Supersession] = []
    for item in supersede:
        if not isinstance(item, Mapping) or not isinstance(item.get("workorder_id"), str):
            raise ValueError("scoper response supersessions must name a workorder_id")
        specs = item.get("replacements", [])
        if not isinstance(specs, list):
            raise ValueError("scoper response replacements must be a list")
        replacements.append(
            Supersession(
                WorkOrderId(item["workorder_id"]),
                tuple(_spec(spec) for spec in specs),
            )
        )
    return ScopingPlan(
        create=tuple(_spec(item) for item in create),
        cancel=tuple(
            WorkOrderId(item)
            for item in _strings(value.get("cancel", []), field="cancel")
        ),
        supersede=tuple(replacements),
        reasons=_strings(value.get("reasons", []), field="reasons"),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Scoper:
    """Invoke one configurable ACP node and decode its proposed scope."""

    agent: str = "codex"
    registry: ACPAgentRegistry | None = None
    node: ScopingNode | None = None

    async def scope(
        self,
        *,
        workorders: Sequence[WorkOrder],
        milestones: Sequence[MilestoneScope],
        policy: ScopingPolicy,
    ) -> ScopingPlan:
        node = self.node or ACPNode(agent=self.agent, registry=self.registry)
        return _plan((await node(_prompt(workorders, milestones, policy))).message)


async def scope(
    *,
    workorders: Sequence[WorkOrder],
    milestones: Sequence[MilestoneScope],
    policy: ScopingPolicy,
) -> ScopingPlan:
    """Return the work-order changes needed to satisfy ``milestones``."""
    return await Scoper().scope(
        workorders=workorders, milestones=milestones, policy=policy
    )


__all__ = ["Scoper", "scope"]
