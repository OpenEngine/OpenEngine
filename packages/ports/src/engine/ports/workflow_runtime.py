"""Workflow Runtime capability.

Durable orchestration: survives process restarts, retries with backoff, and
replays deterministically. Temporal is the intended first implementation, but
nothing in this signature says so -- a local in-memory driver satisfies it too,
which is what makes the engine testable without infrastructure.
"""

from typing import Protocol, runtime_checkable

from engine.domain.events import Event
from engine.domain.ids import RunId


@runtime_checkable
class WorkflowRuntime(Protocol):
    """Starts, resumes, and schedules durable runs."""

    async def start_run(self, run_id: RunId, initial_event: Event) -> None:
        """Begin a durable run. Idempotent on `run_id`."""
        ...

    async def signal_run(self, run_id: RunId, event: Event) -> None:
        """Deliver an event to a run already in flight."""
        ...

    async def schedule_timer(self, run_id: RunId, delay_seconds: float, reason: str) -> None:
        """Wake the run after `delay_seconds`, durably."""
        ...


__all__ = ["WorkflowRuntime"]
