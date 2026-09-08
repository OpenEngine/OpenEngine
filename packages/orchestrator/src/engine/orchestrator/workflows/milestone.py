"""Milestone work-order scoping workflow."""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.domain import MilestoneScope, ScopingPlan, ScopingPolicy, WorkOrder
from engine.scoper import Scoper


@dataclass(slots=True)
class MilestoneWorkflow:
    """Ask the ACP-backed scoper for the work needed by one milestone.

    Keeping the scoper behind the workflow boundary gives Temporal one durable
    operation to schedule while the web surface can exercise the same operation
    in process. ``Scoper`` owns the provider-neutral ``ACPNode`` invocation and
    the conversion of the agent's JSON answer into a ``ScopingPlan``.
    """

    scoper: Scoper = field(default_factory=Scoper)

    async def run(
        self,
        *,
        workorders: tuple[WorkOrder, ...],
        milestone: MilestoneScope,
        policy: ScopingPolicy,
    ) -> ScopingPlan:
        """Return proposed create, cancel, and supersede operations."""
        return await self.scoper.scope(
            workorders=workorders,
            milestones=(milestone,),
            policy=policy,
        )
