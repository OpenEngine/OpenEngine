"""Run the Graph Engine (ge).

It connects to the NATS at `OE_NATS_URL`, serves its query and control
subjects, and answers `GET /healthz` on 127.0.0.1 with 200 only while it is
connected and subscribed. Once it is, it writes the health URL to
`OE_GE_READY_FILE`, if set. On SIGTERM it stops taking requests, answers the
ones it has, and exits 0.

Only `graphs.list` is served, as an empty page until the graph registry exists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import nats
from openengine.messaging import Request, Route, Server, listen_address
from openengine.messaging.server import is_loopback
from openengine.persistence import DatabaseConfig
from openengine.telemetry import configure

from graph_engine.persistence import PERSISTENCE

NATS_URL_VARIABLE = "OE_NATS_URL"
HEALTH_PORT_VARIABLE = "OE_GE_HEALTH_PORT"
READY_FILE_VARIABLE = "OE_GE_READY_FILE"
HEALTH_HOST = "127.0.0.1"
CONNECT_TIMEOUT_SECONDS = 5.0
EXIT_OK = 0
EXIT_FAILED = 1

log = logging.getLogger("graph_engine")


async def list_graphs(request: Request) -> dict[str, Any]:
    return {"items": [], "next_cursor": None}


QUERIES = {
    "graphs.list": Route(list_graphs, "graph_engine/v1/schemas/queries.schema.json#/$defs/list_graphs_query"),
}


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


async def serve(nats_url: str, health_port: int, ready_file: Path | None) -> int:
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
    server = Server(connection, "ge", listener, queries=QUERIES)
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
    await server.drain()
    await connection.drain()
    health.close()
    if ready_file:
        ready_file.unlink(missing_ok=True)
    log.info("stopped")
    return EXIT_OK


def main() -> int:
    telemetry = configure("ge")
    try:
        with telemetry.tracer(__name__).start_as_current_span("ge boot"):
            log.info("starting")
            config = DatabaseConfig.from_environment()
            database = PERSISTENCE.database(config)
            log.info(
                "database configured",
                extra={"db.system.name": config.backend.value, "db.namespace": PERSISTENCE.name,
                       "db.url": database.url(asynchronous=False).render_as_string(hide_password=True)},
            )
        ready_file = os.environ.get(READY_FILE_VARIABLE)
        return asyncio.run(
            serve(
                os.environ.get(NATS_URL_VARIABLE, "nats://127.0.0.1:4222"),
                int(os.environ.get(HEALTH_PORT_VARIABLE, "0")),
                Path(ready_file) if ready_file else None,
            )
        )
    except OSError:
        log.exception("failed")
        return EXIT_FAILED
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    sys.exit(main())
