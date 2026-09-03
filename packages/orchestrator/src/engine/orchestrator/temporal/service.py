"""Temporal subservice lifecycle and workflow registration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class TemporalService:
    """Own the Temporal service lifecycle for an orchestrator process."""

    def __init__(
        self,
        target_host: str = "localhost:7233",
        *,
        namespace: str = "default",
        task_queue: str = "engine",
    ) -> None:
        self.target_host = target_host
        self.namespace = namespace
        self.task_queue = task_queue
        self._workflows: list[type[Any]] = []

    @property
    def workflows(self) -> tuple[type[Any], ...]:
        """Workflow types registered for the next worker boot."""
        return tuple(self._workflows)

    def register_workflow(self, workflow: type[Any]) -> None:
        """Register one workflow type, idempotently."""
        if workflow not in self._workflows:
            self._workflows.append(workflow)

    def register_workflows(self, workflows: Iterable[type[Any]]) -> None:
        """Register workflow types for the Temporal worker."""
        for workflow in workflows:
            self.register_workflow(workflow)

    async def start(self) -> None:
        """Connect to Temporal and boot a worker for registered workflows."""
        raise NotImplementedError("Temporal service startup is not implemented yet")

    async def stop(self) -> None:
        """Stop the Temporal worker and release its client."""
        raise NotImplementedError("Temporal service shutdown is not implemented yet")

    async def start_workflow(
        self, workflow: type[Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Submit a new run of a registered workflow."""
        raise NotImplementedError("Temporal workflow submission is not implemented yet")
