"""Internal workspace-runtime protocol over Core NATS request/reply.

Commands and inspect requests go through the shared messaging client's
envelope-free `send`, one bounded attempt per call: the durable dispatcher, not
the client, owns retries. Updates arrive on `oe.internal.v1.vcp.runtimes.*.updates`,
outside the public `oe.v1.vcp.*` subjects the shared Server and its Authorizer
serve. Production must scope these subjects with per-runtime broker credentials
(see the runtime contract); a shared local account is only a test fixture.
"""

from __future__ import annotations

import logging

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from openengine.messaging import Client

from virtual_control_plane.application.runtime_delivery import RuntimeUpdates

UPDATES = "oe.internal.v1.vcp.runtimes.*.updates"
QUEUE = "vcp-runtime"
REQUEST_TIMEOUT_SECONDS = 5.0

log = logging.getLogger(__name__)


def reply_inbox(subject: str) -> str:
    """The only reply prefix the runtime that publishes on `subject` may name."""
    return f"_INBOX.oe.runtime.{subject.split('.')[5]}."


def span_name(subject: str) -> str:
    """Name spans by template so runtime IDs never name one."""
    tokens = subject.split(".")
    if len(tokens) == 7 and tokens[4] == "runtimes":
        tokens[5] = "{runtime_id}"
    return ".".join(tokens)


class NatsRuntimeTransport:
    def __init__(self, connection: NATS, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._client = Client(connection, timeout=timeout, attempts=1)

    async def send(self, subject: str, wire: bytes) -> bytes:
        return await self._client.send(subject, wire, name=span_name(subject))


class RuntimeUpdateListener:
    """Answers each well-formed update with the handler's committed or rejected acknowledgement.

    VCP may publish to every runtime's commands and replies, so an update is
    answered only on its sender's own inbox: a reply subject naming another
    runtime's command or inbox is never published to.
    """

    def __init__(self, connection: NATS, updates: RuntimeUpdates) -> None:
        self.connection, self.updates = connection, updates
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = await self.connection.subscribe(UPDATES, queue=QUEUE, cb=self._receive)
        await self.connection.flush()

    async def _receive(self, message: Msg) -> None:
        if not message.reply:
            return
        if not message.reply.startswith(reply_inbox(message.subject)):
            log.warning("runtime update reply is outside its runtime's inbox",
                        extra={"messaging.destination.name": message.subject})
            return
        try:
            reply = await self.updates.receive(message.subject, message.data)
        except Exception:
            # No reply: the daemon keeps the update and retries it unchanged.
            log.exception("runtime update not acknowledged", extra={"messaging.destination.name": message.subject})
            return
        if reply is not None:
            await message.respond(reply)

    async def close(self) -> None:
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None
