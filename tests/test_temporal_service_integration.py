"""Integration coverage for the local Temporal service."""

import asyncio
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from engine.orchestrator import TemporalService


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def _start_service(service: TemporalService) -> None:
    # The SDK downloads its dev server on first use. Retry transient 503s and
    # incomplete archives without hiding server startup or recovery failures.
    for attempt in range(3):
        try:
            await service.start()
        except RuntimeError as error:
            message = str(error)
            retryable = (
                "failed to download ephemeral server executable" in message
                and "503 Service Unavailable" in message
            ) or (
                "invalid ephemeral server download: could not extract tar archive entry"
                in message
            )
            if not retryable or attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
        else:
            return


@pytest.mark.parametrize("failure", ["download", "startup", "persistent"])
@pytest.mark.parametrize("download_message", [
    "failed to download ephemeral server executable: "
    "HTTP status server error (503 Service Unavailable)",
    "invalid ephemeral server download: could not extract tar archive entry",
])
def test_start_service_retries_only_transient_download_failure(
    monkeypatch, failure, download_message
):
    download_error = RuntimeError(f"Failed starting Temporal dev server: {download_message}")
    error = RuntimeError("server startup failed") if failure == "startup" else download_error
    service = TemporalService()
    start = AsyncMock(side_effect=[error, None] if failure == "download" else error)
    monkeypatch.setattr(service, "start", start)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    if failure == "download":
        asyncio.run(_start_service(service))
        assert start.await_count == 2
        sleep.assert_awaited_once_with(1)
    else:
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(_start_service(service))
        assert raised.value is error
        assert start.await_count == (3 if failure == "persistent" else 1)
        assert sleep.await_count == (2 if failure == "persistent" else 0)


@pytest.mark.integration
def test_sqlite_temporal_boots_is_accessible_and_recovers(tmp_path: Path) -> None:
    async def exercise_service() -> None:
        database = tmp_path / "temporal.sqlite3"
        service = TemporalService(
            f"127.0.0.1:{_unused_port()}",
            database=database,
            health_check_interval=0.1,
        )
        await _start_service(service)
        try:
            assert await service.is_healthy()
            assert database.exists()

            original_boot = service._boot
            reboot_attempts = 0

            async def fail_first_reboot() -> None:
                nonlocal reboot_attempts
                reboot_attempts += 1
                if reboot_attempts == 1:
                    raise RuntimeError("transient restart failure")
                await original_boot()

            service._boot = fail_first_reboot
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
            assert reboot_attempts == 2
        finally:
            await service.stop()

    asyncio.run(exercise_service())
