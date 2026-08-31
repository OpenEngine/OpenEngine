"""Data exchanged across the work-order scoping boundary."""

from dataclasses import dataclass, field
from enum import Enum

from engine.domain.ids import MilestoneId, WorkOrderId


class WorkOrderStatus(Enum):
    """Completion state visible to the scoper."""

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


@dataclass(frozen=True, slots=True)
class WorkOrderSpec:
    """A work order the scoper proposes creating."""

    milestone_id: MilestoneId
    name: str
    objective: str
    evidence_requirements: tuple[str, ...] = field(default=())
    dependencies: tuple[WorkOrderId, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class WorkOrder:
    """Existing work and its current completion state."""

    workorder_id: WorkOrderId
    spec: WorkOrderSpec
    status: WorkOrderStatus = WorkOrderStatus.PENDING


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


__all__ = [
    "MilestoneScope",
    "ScopingPlan",
    "ScopingPolicy",
    "Supersession",
    "WorkOrder",
    "WorkOrderSpec",
    "WorkOrderStatus",
]
