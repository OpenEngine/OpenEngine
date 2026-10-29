"""Probe the authenticated broker protocol; HTTP and files cannot identify it."""

from __future__ import annotations

import asyncio
import ssl
import subprocess
from collections.abc import Callable

from nats.errors import Error as NatsError

from openengine.messaging.broker import BrokerTrust, connect
from openengine.supervisor.readiness.http import Cancelled, ExitedBeforeReady, NotReady, ReadyTimeout


class BrokerVerificationFailed(NotReady):
    def __init__(self, error: Exception | str) -> None:
        super().__init__(f"broker verification failed: {error}")


def _check(process: subprocess.Popen, cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise Cancelled("nats-server")
    if (code := process.poll()) is not None:
        raise ExitedBeforeReady("nats-server", code)


async def _probe(trust: BrokerTrust) -> None:
    client = await connect(trust.url, trust=trust, name="oe-supervisor", allow_reconnect=False, connect_timeout=1)
    try:
        await client.flush(timeout=1)
    finally:
        await client.close()


async def _wait(process: subprocess.Popen, trust: BrokerTrust, timeout: float, cancelled: Callable[[], bool]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error = "broker did not accept a connection"
    while loop.time() < deadline:
        _check(process, cancelled)
        task = asyncio.create_task(_probe(trust))
        try:
            while not task.done():
                _check(process, cancelled)
                if loop.time() >= deadline:
                    raise ReadyTimeout("nats-server", timeout)
                await asyncio.wait({task}, timeout=min(0.05, max(0, deadline - loop.time())))
            await task
        except ssl.SSLError as error:
            raise BrokerVerificationFailed(error) from error
        except (OSError, NatsError, TimeoutError) as error:
            last_error = str(error)
        else:
            _check(process, cancelled)
            return
        finally:
            if not task.done():
                task.cancel()
                # Bound cancellation; closing the dedicated event loop below
                # never waits indefinitely for transport cooperation.
                done, _ = await asyncio.wait({task}, timeout=6)
                if not done:
                    task.add_done_callback(lambda result: None if result.cancelled() else result.exception())
            elif not task.cancelled():
                task.exception()
        await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))
    raise BrokerVerificationFailed(last_error)


def wait_for_broker(
    process: subprocess.Popen, trust: BrokerTrust, *, timeout: float, cancelled: Callable[[], bool],
) -> None:
    # asyncio.run waits indefinitely for cancellation-resistant tasks on exit.
    # This short-lived probe loop has a bounded teardown; children are reaped
    # by the supervisor even when the transport fails to cooperate.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_wait(process, trust, timeout, cancelled))
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=1))
        loop.close()
