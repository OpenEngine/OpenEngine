"""Run the Virtual Control Plane (vcp).

It connects to the NATS at `OE_NATS_URL`, takes exclusive ownership of its
state directory (`OE_VCP_STATE_DIR`), serves its query and control subjects
through the shared `openengine.messaging` server -- so authorization runs
before any handler, the same as every other V2 service -- and answers
`GET /healthz` on 127.0.0.1 with 200 only while it is connected, holds that
lock and is subscribed. Once it is, it writes the health URL to
`OE_VCP_READY_FILE`, if set. On SIGTERM it stops taking requests, answers the
ones it can finish within the drain budget, and releases the lock after the
event loop stops. A successful close exits 0; a failed final close exits
non-zero. Shutdown never destroys a workspace.

It migrates its database at startup and, while running, publishes committed
outbox events to the `vcp-events` JetStream stream (`application/outbox.py`);
shutdown stops that worker before closing the connection, leaving unpublished
events for the next start.

Workspace creation, `ensure` and lifecycle queries use durable receipts and a
restartable operation worker. The smolvm adapter is registered only when
`OE_VCP_SMOLVM_IMAGE` names a guest image (`OE_VCP_SMOLVM_TOOLS` and
`OE_VCP_SMOLVM_EXECUTABLE` refine it); otherwise no provider is configured and
readiness promises request handling, not compute or an executor. See the
service README.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import nats
from alembic.util import CommandError
from openengine.messaging import Server, listen_address
from openengine.messaging.server import is_loopback
from openengine.persistence import DatabaseConfig
from openengine.telemetry import configure
from sqlalchemy.exc import SQLAlchemyError

from virtual_control_plane.application.lifecycle import Lifecycle
from virtual_control_plane.application.ports import AllocationProvider
from virtual_control_plane.application.outbox import OutboxPublisher
from virtual_control_plane.exclusion import AlreadyRunning, StateDirectory
from virtual_control_plane.jetstream import JetStreamEvents
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.providers.smolvm import DEFAULT_TOOLS, Smolvm

NATS_URL_VARIABLE = "OE_NATS_URL"
HEALTH_PORT_VARIABLE = "OE_VCP_HEALTH_PORT"
READY_FILE_VARIABLE = "OE_VCP_READY_FILE"
STATE_DIR_VARIABLE = "OE_VCP_STATE_DIR"
HEALTH_HOST = "127.0.0.1"
CONNECT_TIMEOUT_SECONDS = 5.0
OUTBOX_CLOSE_SECONDS = 5.0
EXIT_OK = 0
EXIT_FAILED = 1

log = logging.getLogger("virtual_control_plane")

SMOLVM_IMAGE_VARIABLE = "OE_VCP_SMOLVM_IMAGE"
SMOLVM_TOOLS_VARIABLE = "OE_VCP_SMOLVM_TOOLS"
SMOLVM_EXECUTABLE_VARIABLE = "OE_VCP_SMOLVM_EXECUTABLE"


def providers(environment: Mapping[str, str] = os.environ) -> dict[str, AllocationProvider]:
    """The managed adapters this host is configured for.

    smolvm is opt-in through its image; #1054's worktree adapter registers here
    too once it exists. Without configuration capabilities.get is honest about
    serving none, rather than advertising unimplemented isolation.
    """
    configured: dict[str, AllocationProvider] = {}
    if image := environment.get(SMOLVM_IMAGE_VARIABLE):
        tools = [tool for tool in environment.get(SMOLVM_TOOLS_VARIABLE, ",".join(DEFAULT_TOOLS)).split(",") if tool]
        configured["smolvm"] = Smolvm(image, tools=tools, executable=environment.get(SMOLVM_EXECUTABLE_VARIABLE, "smolvm"))
    return configured


async def _health(healthy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
        found = request.split(b" ", 2)[1:2] == [b"/healthz"]
        status = b"200 OK" if found and healthy() else b"404 Not Found" if not found else b"503 Service Unavailable"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
    except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        writer.close()


async def serve(nats_url: str, health_port: int, ready_file: Path | None, store: DurableStore) -> int:
    stopping = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopping.set)
    connecting = asyncio.ensure_future(
        nats.connect(nats_url, name="vcp", connect_timeout=CONNECT_TIMEOUT_SECONDS, max_reconnect_attempts=-1)
    )
    # nats-py retries a first connection forever, so a SIGTERM must be able to end the wait.
    stopped = asyncio.ensure_future(stopping.wait())
    await asyncio.wait([connecting, stopped], return_when=asyncio.FIRST_COMPLETED)
    stopped.cancel()
    if not connecting.done():
        connecting.cancel()
        log.info("stopped before NATS connected", extra={"server.address": nats_url})
        return EXIT_OK
    try:
        connection = connecting.result()
    except (OSError, nats.errors.Error):
        log.exception("could not connect to NATS", extra={"server.address": nats_url})
        return EXIT_FAILED
    listener = listen_address(connection)
    log.info("nats connected", extra={"server.address": nats_url, "server.listen": listener})
    if not is_loopback(urlsplit(listener).hostname):
        log.warning("NATS listens beyond loopback; local-operator mode refuses every request",
                    extra={"server.listen": listener})
    lifecycle = Lifecycle(store, providers())
    log.info("providers configured", extra={"vcp.providers": sorted(lifecycle.providers)})
    server = Server(connection, "vcp", listener, queries=lifecycle.queries(), commands=lifecycle.commands())
    publisher = OutboxPublisher(store, JetStreamEvents(connection.jetstream()))
    health = None
    try:
        await server.start()
        publisher.start()
        lifecycle.start()
        health = await asyncio.start_server(
            lambda reader, writer: _health(
                lambda: connection.is_connected and not stopping.is_set(), reader, writer
            ),
            HEALTH_HOST,
            health_port,
        )
        url = f"http://{HEALTH_HOST}:{health.sockets[0].getsockname()[1]}/healthz"
        if ready_file:
            ready_file.write_text(url)
        log.info("ready", extra={"url.full": url})
        await stopping.wait()
        log.info("draining")
    finally:
        stopping.set()
        try:
            # Withdraw startup readiness before waiting for in-flight replies.
            if ready_file:
                ready_file.unlink(missing_ok=True)
        finally:
            try:
                # Broker I/O must stop before the connection closes; pending events survive.
                await lifecycle.close()
                await publisher.close(OUTBOX_CLOSE_SECONDS)
                await server.close()
            finally:
                if health is not None:
                    # Do not wait on health clients during shutdown; the event
                    # loop cancels them before main releases owned resources.
                    health.close()
    log.info("stopped")
    return EXIT_OK


async def _serve_store(nats_url: str, health_port: int, ready_file: Path | None, store: DurableStore) -> int:
    try:
        return await serve(nats_url, health_port, ready_file, store)
    finally:
        await store.close()


def _state_directory() -> Path:
    directory = os.environ.get(STATE_DIR_VARIABLE)
    return Path(directory) if directory else Path.cwd() / ".openengine" / "vcp"


def main() -> int:
    telemetry = configure("vcp")
    state = StateDirectory(_state_directory())
    try:
        with telemetry.tracer(__name__).start_as_current_span("vcp boot"):
            log.info("starting")
            try:
                state.acquire()
            except AlreadyRunning:
                log.exception("another vcp process already owns %s", state.directory)
                return EXIT_FAILED
            log.info("state directory owned", extra={"vcp.state_directory": str(state.directory)})
            config = DatabaseConfig.from_environment()
            database = PERSISTENCE.database(config)
            upgrade = PERSISTENCE.upgrade(config)
            log.info(
                "database migrated",
                extra={"db.system.name": config.backend.value, "db.namespace": PERSISTENCE.name,
                       "db.url": database.url(asynchronous=False).render_as_string(hide_password=True),
                       "db.revision": upgrade.after},
            )
        ready_file = os.environ.get(READY_FILE_VARIABLE)
        return asyncio.run(
            _serve_store(
                os.environ.get(NATS_URL_VARIABLE, "nats://127.0.0.1:4222"),
                int(os.environ.get(HEALTH_PORT_VARIABLE, "0")),
                Path(ready_file) if ready_file else None,
                DurableStore(database),
            )
        )
    except (OSError, CommandError, SQLAlchemyError, nats.errors.Error):
        # An outage can close cleanly; a failed final close must remain a failure.
        log.exception("failed")
        return EXIT_FAILED
    finally:
        # A workspace a provider owns on disk or as a VM outlives this process;
        # releasing the lock never tears one down.
        state.release()
        telemetry.shutdown()


if __name__ == "__main__":
    sys.exit(main())
