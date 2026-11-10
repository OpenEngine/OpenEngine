"""Send queries and commands and wait, for a bounded time, for their replies.

Each attempt waits `timeout` seconds. A timeout means the request or its
reply was lost, so the outcome is unknown: the client sends the same bytes
again, so a command keeps its `command_id` and `causal_path`, up to
`attempts` times. No responders means nothing serves the subject, and a
closed or draining connection means NATS went away; both are reported at once
as Unavailable, as retrying cannot help.

`send` is the envelope-free form for internal protocols with their own
durable acknowledgements: it publishes caller-supplied bytes to the exact
subject, unchanged on every attempt, and returns the reply's bytes undecoded.
It adds no command ID or causal hop and never interprets the reply, so a
persisted body and its acknowledgement keep their protocol's meaning. Its
bounded retries do not replace a durable outbox.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from nats.errors import (
    ConnectionClosedError,
    ConnectionDrainingError,
    NoRespondersError,
    StaleConnectionError,
    TimeoutError as NatsTimeoutError,
)

from openengine.messaging import envelopes
from openengine.messaging.errors import ServiceError, Unavailable
from openengine.telemetry import sending

DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_ATTEMPTS = 3

log = logging.getLogger(__name__)


class Reply(Protocol):
    data: bytes


class Requester(Protocol):
    """The part of a nats-py client the messaging client uses."""

    async def request(self, subject: str, payload: bytes = b"", timeout: float = 0.5, old_style: bool = False,
                      headers: dict[str, Any] | None = None) -> Reply: ...


class Client:
    def __init__(self, nats: Requester, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, attempts: int = DEFAULT_ATTEMPTS) -> None:
        self._nats = nats
        self.timeout = timeout
        self.attempts = attempts

    async def query(self, subject: str, params: dict[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        """The whole query-response; raises ServiceError when its status is `error`."""
        reply = await self._request(subject, envelopes.encode(envelopes.query(params, request_id=request_id)))
        if reply.get("status") != "ok":
            raise _error(subject, reply)
        return reply

    async def command(self, subject: str, payload: dict[str, Any], **identity: Any) -> dict[str, Any]:
        """The command-response; raises ServiceError when the command is rejected.

        `identity` is `command_id`, `caused_by`, `expected_version` and `deadline_at`, as `envelopes.command` takes.
        """
        reply = await self._request(subject, envelopes.encode(envelopes.command(payload, **identity)))
        if reply.get("status") != "accepted":
            raise _error(subject, reply)
        return reply

    async def send(self, subject: str, data: bytes, *, name: str | None = None) -> bytes:
        """The reply's exact bytes to `data`, sent unchanged to `subject` on each attempt.

        `name` is the span name, for subjects whose tokens are identities.
        """
        return await self._exchange(subject, data, name=name)

    async def _request(self, subject: str, body: bytes) -> dict[str, Any]:
        reply = envelopes.decode(await self._exchange(subject, body))
        if not isinstance(reply, dict):
            raise ServiceError("INTERNAL", f"{subject} replied with {type(reply).__name__}, not an object")
        return reply

    async def _exchange(self, subject: str, body: bytes, *, name: str | None = None) -> bytes:
        for attempt in range(1, self.attempts + 1):
            try:
                with sending(subject, name=name) as headers:
                    message = await self._nats.request(subject, body, timeout=self.timeout, headers=headers)
            except NoRespondersError:
                raise Unavailable(subject, "no responders", no_responders=True) from None
            except (ConnectionClosedError, ConnectionDrainingError, StaleConnectionError) as lost:
                raise Unavailable(subject, f"NATS connection lost: {type(lost).__name__}") from None
            except NatsTimeoutError:
                log.warning("no reply in time", extra={"messaging.destination.name": subject, "attempt": attempt})
                continue
            return message.data
        raise Unavailable(subject, f"no reply after {self.attempts} attempts of {self.timeout:g}s")


def _error(subject: str, reply: dict[str, Any]) -> ServiceError:
    if isinstance(error := reply.get("error"), dict) and {"code", "message", "retryable"} <= error.keys():
        return ServiceError.from_dict(error)
    return ServiceError("INTERNAL", f"{subject} replied with status {reply.get('status')!r} and no error")
