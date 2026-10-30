"""Send queries and commands and wait, for a bounded time, for their replies.

Each attempt waits `timeout` seconds. A timeout means the request or its
reply was lost, so the outcome is unknown: the client sends the same bytes
again, so a command keeps its `command_id` and `causal_path`, up to
`attempts` times. No responders means nothing serves the subject, and a
closed or draining connection means NATS went away; both are reported at once
as Unavailable, as retrying cannot help.
"""

from __future__ import annotations

import logging
import math
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
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout must be finite and greater than 0, at most 60 seconds")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
            raise ValueError("attempts must be an integer between 1 and 10")
        self._nats = nats
        self.timeout = timeout
        self.attempts = attempts

    async def query(self, subject: str, params: dict[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
        """The whole query-response; raises ServiceError when its status is `error`."""
        body = envelopes.query(params, request_id=request_id)
        reply = await self._request(subject, body, envelopes.QUERY_RESPONSE, "request_id")
        if reply.get("status") != "ok":
            raise _error(subject, reply)
        return reply

    async def command(self, subject: str, payload: dict[str, Any], **identity: Any) -> dict[str, Any]:
        """The command-response; raises ServiceError when the command is rejected.

        `identity` is `command_id`, `caused_by`, `expected_version` and `deadline_at`, as `envelopes.command` takes.
        """
        body = envelopes.command(payload, **identity)
        reply = await self._request(subject, body, envelopes.COMMAND_RESPONSE, "command_id")
        if reply.get("status") != "accepted":
            raise _error(subject, reply)
        return reply

    async def _request(self, subject: str, request: dict[str, Any], schema: str, identity: str) -> dict[str, Any]:
        body = envelopes.encode(request)
        for attempt in range(1, self.attempts + 1):
            try:
                with sending(subject) as headers:
                    message = await self._nats.request(subject, body, timeout=self.timeout, headers=headers)
            except NoRespondersError:
                raise Unavailable(subject, "no responders", no_responders=True) from None
            except (ConnectionClosedError, ConnectionDrainingError, StaleConnectionError) as lost:
                raise Unavailable(subject, f"NATS connection lost: {type(lost).__name__}") from None
            except NatsTimeoutError:
                log.warning("no reply in time", extra={"messaging.destination.name": subject, "attempt": attempt})
                continue
            try:
                reply = envelopes.decode(message.data)
                envelopes.check_response(schema, reply)
                if reply.get(identity) != request[identity]:
                    raise ServiceError("INTERNAL", f"{identity} does not match the request")
            except (ServiceError, UnicodeError) as error:
                raise ServiceError("INTERNAL", f"{subject}: invalid service response ({error}). Check `oe logs` and service versions.") from None
            return reply
        raise Unavailable(subject, f"no reply after {self.attempts} attempts of {self.timeout:g}s")


def _error(subject: str, reply: dict[str, Any]) -> ServiceError:
    if isinstance(error := reply.get("error"), dict) and {"code", "message", "retryable"} <= error.keys():
        return ServiceError.from_dict(error)
    return ServiceError("INTERNAL", f"{subject} replied with status {reply.get('status')!r} and no error")
