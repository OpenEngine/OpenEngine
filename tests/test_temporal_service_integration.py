"""Integration coverage for the local Temporal service."""

import asyncio
import socket
from pathlib import Path

import pytest

from engine.orchestrator import TemporalService


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.integration
def test_sqlite_temporal_boots_is_accessible_and_recovers(tmp_path: Path) -> None:
    async def exercise_service() -> None:
        database = tmp_path / "temporal.sqlite3"
        service = TemporalService(
            f"127.0.0.1:{_unused_port()}",
            database=database,
            health_check_interval=0.1,
        )
        await service.start()
        try:
            assert await service.is_healthy()
            assert database.exists()

            original_environment = service._environment
            assert original_environment is not None
            await original_environment.shutdown()

            async with asyncio.timeout(20):
                while (
                    service._environment is original_environment
                    or not await service.is_healthy()
                ):
                    await asyncio.sleep(0.05)

            assert await service.is_healthy()
        finally:
            await service.stop()

    asyncio.run(exercise_service())
