"""Workflow Runtime capability, backed by Temporal.

Placeholder for Ticket 1. Satisfies `engine.ports.WorkflowRuntime` structurally;
no `temporalio` import yet, and no client, worker, or workflow definition.
"""

from engine.domain.events import Event
from engine.domain.ids import RunId


class TemporalWorkflowRuntime:
    """Durable run orchestration on Temporal.

    Implements `engine.ports.WorkflowRuntime`.
    """

    def __init__(self, target_host: str, namespace: str = "default", task_queue: str = "engine") -> None:
        self._target_host = target_host
        self._namespace = namespace
        self._task_queue = task_queue

    async def start_run(self, run_id: RunId, initial_event: Event) -> None:
        raise NotImplementedError("Temporal workflow start lands with the workflow ticket")

    async def signal_run(self, run_id: RunId, event: Event) -> None:
        raise NotImplementedError("Temporal signal handling lands with the workflow ticket")

    async def schedule_timer(self, run_id: RunId, delay_seconds: float, reason: str) -> None:
        raise NotImplementedError("Temporal timers land with the workflow ticket")


__all__ = ["TemporalWorkflowRuntime"]
