"""Data exchanged across the work-order scoping boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum

from engine.domain.ids import MilestoneId, WorkOrderId


class WorkOrderStatus(Enum):
    """Completion state visible to the scoper."""

    SCHEDULED = "scheduled"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MilestoneScope:
    """The desired state a milestone's work orders must satisfy."""

    milestone_id: MilestoneId
    requirements: tuple[str, ...] = field(default=())
    evidence_requirements: tuple[str, ...] = field(default=())
    dependencies: tuple[MilestoneId, ...] = field(default=())
    name: str = ""
    """Human-readable identity supplied alongside the durable milestone id."""
    source_refs: tuple[TicketSourceRef, ...] = ()
    """Caller-supplied GitHub milestone and issue identities backing this scope."""


class TicketLayer(Enum):
    CONTRACTS = "contracts"
    DATA = "data"
    API = "api"
    FRONTEND = "frontend"


class TicketApproval(Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class TicketSourceKind(Enum):
    GITHUB_MILESTONE = "github_milestone"
    GITHUB_ISSUE = "github_issue"


@dataclass(frozen=True, slots=True)
class TicketSourceRef:
    kind: TicketSourceKind
    repository: str
    number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", TicketSourceKind(self.kind))
        if not isinstance(self.repository, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", self.repository
        ) or self.repository.split("/")[-1] in {".", ".."}:
            raise ValueError("source repository must be owner/repo")
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("source number must be a positive integer")


@dataclass(frozen=True, slots=True)
class WorkOrderSpec:
    """A proposed ticket; optional sizing/layer fields preserve older callers.

    ``dependencies`` names existing work orders; ``dependency_keys`` and
    ``parent_key`` name tickets within this proposal. Size counts changed lines.
    """

    milestone_id: MilestoneId
    name: str
    objective: str
    evidence_requirements: tuple[str, ...] = field(default=())
    dependencies: tuple[WorkOrderId, ...] = field(default=())
    key: str = ""
    layer: TicketLayer | None = None
    estimated_changed_lines: int | None = None
    acceptance_criteria: tuple[str, ...] = ()
    dependency_keys: tuple[str, ...] = ()
    parent_key: str | None = None
    approval: TicketApproval = TicketApproval.PROPOSED
    source_ref: TicketSourceRef | None = None

    def __post_init__(self) -> None:
        if self.layer is not None:
            object.__setattr__(self, "layer", TicketLayer(self.layer))
        object.__setattr__(self, "approval", TicketApproval(self.approval))
        size = self.estimated_changed_lines
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("estimated_changed_lines must be a nonnegative integer")
        if not isinstance(self.key, str):
            raise ValueError("key must be a string")
        if self.parent_key is not None and (
            not isinstance(self.parent_key, str) or not self.parent_key
        ):
            raise ValueError("parent_key must be a nonempty string")
        for name in ("acceptance_criteria", "dependency_keys"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{name} must contain nonempty strings")
            object.__setattr__(self, name, tuple(values))


@dataclass(frozen=True, slots=True)
class WorkOrder:
    """Existing work and its current completion state."""

    workorder_id: WorkOrderId
    spec: WorkOrderSpec
    status: WorkOrderStatus = WorkOrderStatus.PENDING
    parent_id: WorkOrderId | None = None
    subtask_ids: tuple[WorkOrderId, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopingPolicy:
    """Caller-supplied rules that constrain scoping decisions."""

    rules: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Supersession:
    """Replace existing work with newly scoped work."""

    workorder_id: WorkOrderId
    replacements: tuple[WorkOrderSpec, ...]


@dataclass(frozen=True, slots=True)
class ScopingPlan:
    """Proposed changes; applying them is outside the scoper boundary."""

    create: tuple[WorkOrderSpec, ...] = field(default=())
    cancel: tuple[WorkOrderId, ...] = field(default=())
    supersede: tuple[Supersession, ...] = field(default=())
    reasons: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class LoopQueueItem:
    """Approved scoped work, with durable dependency and hierarchy identities."""

    loop_id: str
    plan_id: str
    ticket: WorkOrder


@dataclass(frozen=True, slots=True)
class PersistedScopingPlan:
    """Original proposal plus resolved tickets carrying current approval state."""

    plan_id: str
    loop_id: str
    plan: ScopingPlan
    tickets: tuple[WorkOrder, ...]

    def queue_items(self) -> tuple[LoopQueueItem, ...]:
        return tuple(
            LoopQueueItem(self.loop_id, self.plan_id, ticket)
            for ticket in self.tickets
            if ticket.spec.approval is TicketApproval.APPROVED
        )

    def with_approval(
        self, ticket_id: WorkOrderId, approval: TicketApproval
    ) -> "PersistedScopingPlan":
        if not any(ticket.workorder_id == ticket_id for ticket in self.tickets):
            raise KeyError(ticket_id)
        return replace(self, tickets=tuple(
            replace(ticket, spec=replace(ticket.spec, approval=approval))
            if ticket.workorder_id == ticket_id else ticket
            for ticket in self.tickets
        ))


def resolve_scoping_plan(
    plan_id: str, loop_id: str, plan: ScopingPlan, ids: tuple[WorkOrderId, ...]
) -> PersistedScopingPlan:
    """Resolve forward local references before any store writes occur.

    Parent keys are the canonical hierarchy input; subtask ids are derived.
    Existing durable dependencies are preserved. Supersession replacements
    share the same key namespace as new tickets. Cancellation/supersession
    intent is persisted without changing running work.
    """
    specs = (*plan.create, *(s for item in plan.supersede for s in item.replacements))
    if len(ids) != len(specs) or len(set(ids)) != len(ids):
        raise ValueError("one unique id is required per ticket")
    keys = {spec.key: identity for spec, identity in zip(specs, ids) if spec.key}
    if len(keys) != sum(bool(spec.key) for spec in specs):
        raise ValueError("duplicate plan-local key")

    def lookup(key: str) -> WorkOrderId:
        if key not in keys:
            raise ValueError(f"unknown plan-local key: {key}")
        return keys[key]

    tickets = tuple(WorkOrder(
        identity,
        replace(spec, dependencies=tuple(dict.fromkeys((
            *spec.dependencies, *(lookup(key) for key in spec.dependency_keys)
        )))),
        parent_id=lookup(spec.parent_key) if spec.parent_key else None,
    ) for spec, identity in zip(specs, ids))
    for relation in (lambda t: t.spec.dependencies,
                     lambda t: (t.parent_id,) if t.parent_id else ()):
        graph = {t.workorder_id: relation(t) for t in tickets}
        visited, active = set(), set()

        def visit(identity: WorkOrderId) -> None:
            if identity in active:
                raise ValueError("cyclic ticket references")
            if identity in visited or identity not in graph:
                return
            active.add(identity)
            for target in graph[identity]:
                visit(target)
            active.remove(identity)
            visited.add(identity)

        for identity in graph:
            visit(identity)
    tickets = tuple(replace(t, subtask_ids=tuple(
        child.workorder_id for child in tickets if child.parent_id == t.workorder_id
    )) for t in tickets)
    return PersistedScopingPlan(plan_id, loop_id, plan, tickets)


__all__ = [
    "TicketLayer",
    "TicketApproval",
    "TicketSourceKind",
    "TicketSourceRef",
    "PersistedScopingPlan",
    "LoopQueueItem",
    "resolve_scoping_plan",
    "MilestoneScope",
    "ScopingPlan",
    "ScopingPolicy",
    "Supersession",
    "WorkOrder",
    "WorkOrderSpec",
    "WorkOrderStatus",
]
