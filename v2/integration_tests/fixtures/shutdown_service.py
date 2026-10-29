"""Inject shutdown faults into the real entrypoint, only in test subprocesses."""

import asyncio
import importlib
import sys
from pathlib import Path
from dataclasses import replace

from nats.aio.client import Client
from nats.aio.transport import TcpTransport
from openengine.messaging import Server

module, operation, mode, directory = sys.argv[1:]
service = importlib.import_module(f"{module}.__main__")
markers = Path(directory)
original_start = Server.start


async def start_with_pending_handler(self):
    # Capture the route after the real entrypoint constructs its registry.
    original = self.queries[operation]

    async def pending(request):
        markers.joinpath("entered").touch()
        try:
            while not markers.joinpath("release").exists():
                await asyncio.sleep(0.01)
            return await original.handler(request)
        finally:
            markers.joinpath("handler-stopped").touch()

    self.queries = {**self.queries, operation: replace(original, handler=pending)}
    await original_start(self)


async def stalled_close(self):
    markers.joinpath("closing").touch()
    await asyncio.Event().wait()


async def failed_close(self):
    raise OSError("injected-close-failure")


if mode == "pending":
    Server.start = start_with_pending_handler
elif mode == "stalled-close":
    TcpTransport.wait_closed = stalled_close
elif mode == "close-error":
    Client.close = failed_close
else:
    raise ValueError(mode)

sys.exit(service.main())
