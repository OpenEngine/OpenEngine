"""Top-level orchestration lifecycle."""

from __future__ import annotations

from typing import Any

from engine.orchestrator.temporal import TemporalService
from engine.orchestrator.workflows import (
    GraphRunWorkflow,
    MilestoneWorkflow,
    PacingWorkflow,
    ProjectWorkflow,
    WorkOrderWorkflow,
)

WORKFLOWS = (
    ProjectWorkflow,
    MilestoneWorkflow,
    PacingWorkflow,
    WorkOrderWorkflow,
    GraphRunWorkflow,
)


class Orchestrator:
    """Register current workflow code and coordinate Temporal runs."""

    def __init__(self, temporal: TemporalService) -> None:
        self.temporal = temporal

    async def start(self) -> None:
        """Register all first-class workflows before starting Temporal."""
        self.temporal.register_workflows(WORKFLOWS)
        await self.temporal.start()

    async def stop(self) -> None:
        """Stop the orchestration subservices."""
        await self.temporal.stop()

    async def submit(self, workflow: type[Any], *args: Any, **kwargs: Any) -> Any:
        """Submit a new workflow run to Temporal."""
        return await self.temporal.start_workflow(workflow, *args, **kwargs)
