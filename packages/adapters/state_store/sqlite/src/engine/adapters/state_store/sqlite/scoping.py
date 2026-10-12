"""JSON representation of a durable scoping proposal and its resolved tickets."""

from dataclasses import asdict
from enum import Enum
import json

from engine.domain.scoping import (
    PersistedScopingPlan, ScopingPlan, Supersession, TicketSourceRef,
    WorkOrder, WorkOrderSpec, WorkOrderStatus,
)


def encode_plan(plan: PersistedScopingPlan) -> str:
    return json.dumps(asdict(plan), default=lambda value: value.value
                      if isinstance(value, Enum) else value)


def decode_plan(payload: str) -> PersistedScopingPlan:
    value = json.loads(payload)

    def spec(raw: dict) -> WorkOrderSpec:
        raw = dict(raw)
        for key in ("evidence_requirements", "dependencies", "acceptance_criteria",
                    "dependency_keys"):
            raw[key] = tuple(raw[key])
        if raw["source_ref"] is not None:
            raw["source_ref"] = TicketSourceRef(**raw["source_ref"])
        return WorkOrderSpec(**raw)

    raw = value["plan"]
    plan = ScopingPlan(
        create=tuple(spec(item) for item in raw["create"]),
        cancel=tuple(raw["cancel"]),
        supersede=tuple(Supersession(item["workorder_id"], tuple(
            spec(replacement) for replacement in item["replacements"]
        )) for item in raw["supersede"]),
        reasons=tuple(raw["reasons"]),
    )
    tickets = tuple(WorkOrder(
        item["workorder_id"], spec(item["spec"]), WorkOrderStatus(item["status"]),
        item["parent_id"], tuple(item["subtask_ids"]),
    ) for item in value["tickets"])
    return PersistedScopingPlan(value["plan_id"], value["loop_id"], plan, tickets)
