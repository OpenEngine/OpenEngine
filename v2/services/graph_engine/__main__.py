"""Run the Graph Engine (ge).

It brings its database to the head of its migrations, connects to the NATS
at `OE_NATS_URL`, serves its query and control subjects, and answers
`GET /healthz` on 127.0.0.1 with 200 only while it is connected and
subscribed. Once it is, it writes the health URL to
`OE_GE_READY_FILE`, if set. On SIGTERM it stops taking requests, answers the
ones it has, and exits 0.

It serves `graphs.apply`, `graphs.list`, `graphs.{graph_id}.get` and the
operations it records (see registry.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit

import nats
from alembic.util import CommandError
from openengine.messaging import LocalOperator, Server, listen_address
from openengine.messaging.server import is_loopback
from openengine.persistence import DatabaseConfig
from openengine.telemetry import configure
from sqlalchemy.exc import SQLAlchemyError

from graph_engine.persistence import PERSISTENCE
from graph_engine.persistence.store import Graphs
from graph_engine.registry import Registry

NATS_URL_VARIABLE = "OE_NATS_URL"
HEALTH_PORT_VARIABLE = "OE_GE_HEALTH_PORT"
READY_FILE_VARIABLE = "OE_GE_READY_FILE"
HEALTH_HOST = "127.0.0.1"
CONNECT_TIMEOUT_SECONDS = 5.0
EXIT_OK = 0
EXIT_FAILED = 1

log = logging.getLogger("graph_engine")


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


async def serve(nats_url: str, health_port: int, ready_file: Path | None, store: Graphs) -> int:
    stopping = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stopping.set)
    connecting = asyncio.ensure_future(
        nats.connect(nats_url, name="ge", connect_timeout=CONNECT_TIMEOUT_SECONDS, max_reconnect_attempts=-1)
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
    # One authorizer for every request, and again for the project each one reaches.
    authorizer = LocalOperator()
    registry = Registry(store, authorizer)
    server = Server(
        connection, "ge", listener, queries=registry.queries(), commands=registry.commands(), authorizer=authorizer,
    )
    health = None
    try:
        await server.start()
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
        try:
            await server.close()
        finally:
            if health is not None:
                health.close()
                await health.wait_closed()
            if ready_file:
                ready_file.unlink(missing_ok=True)
    log.info("stopped")
    return EXIT_OK


async def _serve_store(nats_url: str, health_port: int, ready_file: Path | None, store: Graphs) -> int:
    try:
        return await serve(nats_url, health_port, ready_file, store)
    finally:
        await store.close()


def main() -> int:
    telemetry = configure("ge")
    try:
        with telemetry.tracer(__name__).start_as_current_span("ge boot"):
            log.info("starting")
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
                PERSISTENCE.store(database),
            )
        )
    except (OSError, CommandError, SQLAlchemyError):
        log.exception("failed")
        return EXIT_FAILED
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    sys.exit(main())
