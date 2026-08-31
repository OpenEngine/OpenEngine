"""Work-order scoping interface.

The implementation will execute a LangGraph graph. This module establishes the
stable input and output contract without prematurely encoding that graph.
"""

from collections.abc import Sequence

from engine.domain import MilestoneScope, ScopingPlan, ScopingPolicy, WorkOrder


def scope(
    *,
    workorders: Sequence[WorkOrder],
    milestones: Sequence[MilestoneScope],
    policy: ScopingPolicy,
) -> ScopingPlan:
    """Return the work-order changes needed to satisfy ``milestones``."""
    raise NotImplementedError("LangGraph scoping implementation is not available yet")


__all__ = ["scope"]
