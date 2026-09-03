"""Temporal subservice lifecycle and workflow registration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

logger = structlog.get_logger()


class TemporalService:
    """Own the Temporal service lifecycle for an orchestrator process."""

    def __init__(
        self,
        target_host: str = "localhost:7233",
        *,
        namespace: str = "default",
        task_queue: str = "engine",
        database: Path | str = Path(".engine/temporal.sqlite3"),
        health_check_interval: float = 5.0,
    ) -> None:
        self.target_host = target_host
        self.namespace = namespace
        self.task_queue = task_queue
        self.database = Path(database)
        self.health_check_interval = health_check_interval
        self._workflows: list[type[Any]] = []
        self._environment: WorkflowEnvironment | None = None
        self._worker: Worker | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._stopping = False

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
        async with self._lifecycle_lock:
            if self._environment is not None:
                return
            self._stopping = False
            await self._boot()
            self._monitor_task = asyncio.create_task(
                self._monitor(), name="temporal-health-monitor"
            )

    async def stop(self) -> None:
        """Stop the Temporal worker and release its client."""
        self._stopping = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        async with self._lifecycle_lock:
            await self._shutdown_runtime()

    async def start_workflow(
        self, workflow: type[Any], *args: Any, **kwargs: Any
    ) -> WorkflowHandle[Any, Any]:
        """Submit a new run of a registered workflow."""
        if workflow not in self._workflows:
            raise ValueError(f"workflow is not registered: {workflow.__name__}")
        client = self.client
        return await client.start_workflow(
            workflow.run,
            args=args,
            task_queue=self.task_queue,
            **kwargs,
        )

    @property
    def client(self) -> Client:
        """Return the connected client once the service has started."""
        if self._environment is None:
            raise RuntimeError("Temporal service is not running")
        return self._environment.client

    async def is_healthy(self) -> bool:
        """Check that Temporal's workflow service is accepting requests."""
        try:
            return await self.client.service_client.check_health(
                timeout=timedelta(seconds=2)
            )
        except Exception:
            return False

    async def _boot(self) -> None:
        host, separator, port_text = self.target_host.rpartition(":")
        if not separator or not host:
            raise ValueError("target_host must use the host:port form")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "temporal.server.starting",
            target_host=self.target_host,
            database=str(self.database),
        )
        self._environment = await WorkflowEnvironment.start_local(
            namespace=self.namespace,
            ip=host,
            port=int(port_text),
            dev_server_database_filename=str(self.database),
            dev_server_log_format="json",
            dev_server_log_level="info",
        )
        logger.info("temporal.server.started", target_host=self.target_host)
        logger.info("temporal.client.connected", namespace=self.namespace)
        if self._workflows:
            logger.info(
                "temporal.worker.starting",
                task_queue=self.task_queue,
                workflow_count=len(self._workflows),
            )
            self._worker = Worker(
                self.client, task_queue=self.task_queue, workflows=self._workflows
            )
            self._worker_task = asyncio.create_task(
                self._worker.run(), name="temporal-worker"
            )
            logger.info("temporal.worker.started", task_queue=self.task_queue)
        else:
            logger.info("temporal.worker.skipped", reason="no_workflows_registered")

    async def _shutdown_runtime(self) -> None:
        if self._worker is not None:
            logger.info("temporal.worker.stopping")
            await self._worker.shutdown()
            if self._worker_task is not None:
                await asyncio.gather(self._worker_task, return_exceptions=True)
            self._worker = None
            self._worker_task = None
        if self._environment is not None:
            logger.info("temporal.server.stopping")
            await self._environment.shutdown()
            self._environment = None
            logger.info("temporal.server.stopped")

    async def _monitor(self) -> None:
        logger.info(
            "temporal.health_monitor.started",
            interval_seconds=self.health_check_interval,
        )
        while True:
            await asyncio.sleep(self.health_check_interval)
            if await self.is_healthy():
                continue
            logger.error("temporal.health_check.failed")
            async with self._lifecycle_lock:
                if self._stopping:
                    return
                logger.info("temporal.server.restarting")
                try:
                    await self._shutdown_runtime()
                    await self._boot()
                except Exception:
                    logger.exception(
                        "temporal.server.restart_failed",
                        retry_in_seconds=self.health_check_interval,
                    )
                    continue
                logger.info("temporal.server.restarted")
