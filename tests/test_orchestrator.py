"""First-class workflow orchestration scaffold."""

import asyncio
from typing import Any

from engine.orchestrator import (
    GraphRunWorkflow,
    MilestoneWorkflow,
    Orchestrator,
    PacingWorkflow,
    ProjectWorkflow,
    TemporalService,
    WorkOrderWorkflow,
)


WORKFLOWS = (
    ProjectWorkflow,
    MilestoneWorkflow,
    PacingWorkflow,
    WorkOrderWorkflow,
    GraphRunWorkflow,
)


class RecordingTemporalService(TemporalService):
    def __init__(self) -> None:
        super().__init__()
        self.started = False
        self.stopped = False
        self.submission: tuple[type[Any], tuple[Any, ...], dict[str, Any]] | None = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def start_workflow(self, workflow: type[Any], *args: Any, **kwargs: Any) -> str:
        self.submission = workflow, args, kwargs
        return "run-id"


def test_temporal_registration_is_idempotent() -> None:
    temporal = TemporalService()

    temporal.register_workflow(ProjectWorkflow)
    temporal.register_workflows(WORKFLOWS)

    assert temporal.workflows == WORKFLOWS


def test_orchestrator_registers_workflows_before_starting() -> None:
    temporal = RecordingTemporalService()
    orchestrator = Orchestrator(temporal)

    asyncio.run(orchestrator.start())

    assert temporal.workflows == WORKFLOWS
    assert temporal.started


def test_orchestrator_delegates_submission_and_shutdown() -> None:
    temporal = RecordingTemporalService()
    orchestrator = Orchestrator(temporal)

    async def submit_and_stop() -> str:
        result = await orchestrator.submit(GraphRunWorkflow, "graph", run_id="run-1")
        await orchestrator.stop()
        return result

    result = asyncio.run(submit_and_stop())

    assert result == "run-id"
    assert temporal.submission == (GraphRunWorkflow, ("graph",), {"run_id": "run-1"})
    assert temporal.stopped
