"""Top-level orchestration lifecycle and command."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from engine.orchestrator.logging import configure_logging
from engine.orchestrator.temporal import TemporalService
from engine.runtime.config import EngineConfigError, load_engine_config

WORKFLOWS: tuple[type[Any], ...] = ()

logger = structlog.get_logger()


class Orchestrator:
    """Register current workflow code and coordinate Temporal runs."""

    def __init__(self, temporal: TemporalService) -> None:
        self.temporal = temporal

    async def start(self) -> None:
        """Register all first-class workflows before starting Temporal."""
        logger.info("orchestrator.registering_workflows", count=len(WORKFLOWS))
        self.temporal.register_workflows(WORKFLOWS)
        await self.temporal.start()
        logger.info("orchestrator.started")

    async def stop(self) -> None:
        """Stop the orchestration subservices."""
        logger.info("orchestrator.stopping")
        await self.temporal.stop()
        logger.info("orchestrator.stopped")

    async def submit(self, workflow: type[Any], *args: Any, **kwargs: Any) -> Any:
        """Submit a new workflow run to Temporal."""
        return await self.temporal.start_workflow(workflow, *args, **kwargs)


async def run(
    *, database: Path, target_host: str, health_check_interval: float
) -> None:
    """Run the orchestrator until an interrupt requests shutdown."""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)

    orchestrator = Orchestrator(
        TemporalService(
            target_host,
            database=database,
            health_check_interval=health_check_interval,
        )
    )
    try:
        await orchestrator.start()
        logger.info("orchestrator.ready", target_host=orchestrator.temporal.target_host)
        await stopped.wait()
    finally:
        await orchestrator.stop()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the SQLite-backed local Temporal orchestrator."""
    parser = argparse.ArgumentParser(description="Run the OpenEngine orchestrator.")
    parser.add_argument("--config")
    parser.add_argument("--host")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--health-check-interval", type=float)
    args = parser.parse_args(argv)
    try:
        loaded = load_engine_config(args.config)
    except EngineConfigError as error:
        parser.error(str(error))
    config = loaded.config.orchestrator
    database = args.database or loaded.orchestrator_database
    target_host = args.host or config.host
    health_check_interval = args.health_check_interval or config.health_check_interval
    configure_logging()
    logger.info("orchestrator.booting")
    asyncio.run(
        run(
            database=database,
            target_host=target_host,
            health_check_interval=health_check_interval,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
