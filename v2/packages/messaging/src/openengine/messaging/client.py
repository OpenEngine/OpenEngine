"""Send queries and commands and wait, for a bounded time, for their replies.

Each attempt waits `timeout` seconds. A timeout means the request or its
reply was lost, so the outcome is unknown: the client sends the same bytes
again, so a command keeps its `command_id` and `causal_path`, up to
`attempts` times. No responders means nothing serves the subject, and a
closed or draining connection means NATS went away; both are reported at once
as Unavailable, as retrying cannot help.

A client takes the subjects of its callers' contracts, the plain
`oe.v1.{svc}.…`. Calling on a tenant's behalf, it is given that tenant's
`Principal` and runs in one of the two tenancy modes `tenancy` describes: in
dedicated mode it sends to the plain subject, which the tenant's account
import carries into the service; in shared mode it is inside the service
account, so it addresses `oe.v1.{account}.{svc}.…` itself with the
principal's account key. Span names use the template `oe.v1.{account}.…`,
never the key.
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
from openengine.messaging.tenancy import Mode, Principal, shared_subject, subject_template
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
    def __init__(
        self, nats: Requester, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, attempts: int = DEFAULT_ATTEMPTS,
        principal: Principal | None = None, tenancy: Mode = Mode.DEDICATED,
    ) -> None:
        if tenancy is Mode.SHARED and (principal is None or principal.account is None):
            raise ValueError("a shared-mode client needs a principal with an account key to address")
        self._nats = nats
        self.timeout = timeout
        self.attempts = attempts
        self.principal = principal
        self.tenancy = tenancy

    def address(self, subject: str) -> tuple[str, str]:
        """Where a plain `oe.v1.{svc}.…` subject is sent in this mode, and the template naming its span."""
        if self.tenancy is Mode.SHARED:
            assert self.principal is not None and self.principal.account is not None
            return shared_subject(subject, self.principal.account), subject_template(subject)
        return subject, subject

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

    async def _request(self, subject: str, body: bytes) -> dict[str, Any]:
        destination, name = self.address(subject)
        for attempt in range(1, self.attempts + 1):
            try:
                with sending(destination, name=name) as headers:
                    message = await self._nats.request(destination, body, timeout=self.timeout, headers=headers)
            except NoRespondersError:
                raise Unavailable(subject, "no responders", no_responders=True) from None
            except (ConnectionClosedError, ConnectionDrainingError, StaleConnectionError) as lost:
                raise Unavailable(subject, f"NATS connection lost: {type(lost).__name__}") from None
            except NatsTimeoutError:
                log.warning("no reply in time", extra={"messaging.destination.name": subject, "attempt": attempt})
                continue
            reply = envelopes.decode(message.data)
            if not isinstance(reply, dict):
                raise ServiceError("INTERNAL", f"{subject} replied with {type(reply).__name__}, not an object")
            return reply
        raise Unavailable(subject, f"no reply after {self.attempts} attempts of {self.timeout:g}s")


def _error(subject: str, reply: dict[str, Any]) -> ServiceError:
    if isinstance(error := reply.get("error"), dict) and {"code", "message", "retryable"} <= error.keys():
        return ServiceError.from_dict(error)
    return ServiceError("INTERNAL", f"{subject} replied with status {reply.get('status')!r} and no error")
