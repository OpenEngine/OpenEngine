"""Work-order scoping backed by an ACP-compatible planning agent."""

from __future__ import annotations

import asyncio
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
from engine.domain.scoping import TicketApproval, TicketLayer, TicketSourceRef
from langgraph_acp import ACPNode, ACPResult
from langgraph_acp.agent import ACPAgentRegistry
from langgraph_acp.providers import CodexACPProvider

ScopingNode = Callable[[str], Awaitable[ACPResult]]

_INSTRUCTIONS = """You are a work-order scoper. Compare the desired milestones with
the existing work orders. Completed and in-progress work must be taken into account;
do not recreate work they already cover. Return only one JSON object with these keys:
create (work-order specs), cancel (work-order ids), supersede (objects containing a
workorder_id and replacement specs), and reasons (strings). A work-order spec has
milestone_id, name, objective, evidence_requirements, and dependencies. Every list
may be empty. Specs may also include key (unique within this plan), layer
(contracts, data, api, frontend), estimated_changed_lines (nonnegative integer),
acceptance_criteria (strings), dependency_keys (plan-local keys), parent_key
(a plan-local key; subtasks are derived), and source_ref ({kind, ref}, with kind
one of github_milestone, github_issue, jira_epic, jira_issue). The dependencies field names
existing durable work-order ids; dependency_keys names tickets in this proposal,
including supersession replacements. All proposals require caller approval.
Do not wrap the JSON in Markdown.

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
        key=value.get("key", ""),
        layer=TicketLayer(value["layer"]) if value.get("layer") is not None else None,
        estimated_changed_lines=value.get("estimated_changed_lines"),
        acceptance_criteria=_strings(value.get("acceptance_criteria", []),
                                     field="acceptance_criteria"),
        dependency_keys=_strings(value.get("dependency_keys", []), field="dependency_keys"),
        parent_key=value.get("parent_key"),
        approval=TicketApproval.PROPOSED,
        source_ref=TicketSourceRef(**value["source_ref"]) if value.get("source_ref") else None,
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
    working_directory: str | None = None
    timeout_seconds: float | None = None

    async def scope(
        self,
        *,
        workorders: Sequence[WorkOrder],
        milestones: Sequence[MilestoneScope],
        policy: ScopingPolicy,
    ) -> ScopingPlan:
        node = self.node or ACPNode(
            agent=self.agent,
            registry=self.registry,
            working_directory=self.working_directory,
        )
        turn = node(_prompt(workorders, milestones, policy))
        result = (
            await turn
            if self.timeout_seconds is None
            else await asyncio.wait_for(turn, timeout=self.timeout_seconds)
        )
        return _plan(result.message)


@dataclass(frozen=True, slots=True)
class MilestoneScoper:
    """In-process first iteration of milestone work-order scoping."""

    scoper: Scoper

    async def run(
        self,
        *,
        workorders: Sequence[WorkOrder],
        milestone: MilestoneScope,
        policy: ScopingPolicy,
    ) -> ScopingPlan:
        return await self.scoper.scope(
            workorders=workorders,
            milestones=(milestone,),
            policy=policy,
        )


def codex_milestone_scoper(
    *,
    binary_path: str,
    working_directory: str,
    timeout_seconds: float | None,
    model: str = "",
) -> MilestoneScoper:
    """Build milestone scoping from the installation's Codex settings."""
    environment = {"CODEX_PATH": binary_path}
    if model:
        environment["CODEX_CONFIG"] = json.dumps({"model": model})
    registry = ACPAgentRegistry(
        (
            CodexACPProvider(env=environment, cwd=working_directory),
        )
    )
    return MilestoneScoper(
        Scoper(
            agent="codex",
            registry=registry,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
        )
    )


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


__all__ = ["MilestoneScoper", "Scoper", "codex_milestone_scoper", "scope"]
