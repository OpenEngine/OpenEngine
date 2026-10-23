"""Serve a service's query and control subjects over NATS request/reply.

The adapter subscribes to `oe.v1.{service}.query.>` and
`oe.v1.{service}.control.>`, and for each request:

1. decodes it and checks its envelope (and the route's own schema, if it
   names one), replying INVALID_ARGUMENT when either is broken;
2. asks the Authorizer, replying PERMISSION_DENIED when it refuses;
3. runs the route's handler, replying with its result or the ServiceError it
   raises. A subject with no route is UNSUPPORTED_CAPABILITY, and any other
   exception INTERNAL.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from openengine.messaging import envelopes
from openengine.messaging.errors import ErrorCode, ServiceError
from openengine.telemetry import receiving

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Request:
    subject: str
    kind: str
    """`query` or `control`."""
    operation: str
    """The subject after `oe.v1.{service}.{kind}.`, such as `graphs.list`."""
    body: dict[str, Any]
    listener: str
    """The address the NATS server the request arrived through listens on."""


class Authorizer(Protocol):
    def authorize(self, request: Request) -> None:
        """Return if the request may proceed; raise a PERMISSION_DENIED ServiceError if not."""


class LocalOperator:
    """Loopback-only local-operator mode, the one `oe up` runs.

    NATS has no accounts here: the local nats-server listens only on loopback,
    so whoever reaches it is the operator on this machine. A request is
    allowed only when the server it arrived through listens on loopback alone,
    as `listen_address` reports it; a server bound to any other address, such
    as 0.0.0.0, is reachable from off the machine, so its requests are
    PERMISSION_DENIED until a non-loopback authorizer exists.
    """

    def authorize(self, request: Request) -> None:
        if not is_loopback(urlsplit(request.listener).hostname):
            raise ServiceError(
                ErrorCode.PERMISSION_DENIED,
                f"local-operator mode serves loopback requests only, not ones through {request.listener}",
                details={"permission": f"{request.kind}.{request.operation}"},
            )


def listen_address(nats: NATS) -> str:
    """The address the connected NATS server listens on, from its INFO.

    This, not the URL used to reach it, is what bounds who else can reach it:
    a server bound to 0.0.0.0 still answers on 127.0.0.1.
    """
    info = nats._server_info  # noqa: SLF001 -- nats-py exposes INFO only here
    host = str(info.get("host", ""))
    return f"nats://{f'[{host}]' if ':' in host else host}:{info.get('port', '')}"


def is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


Handler = Callable[[Request], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Route:
    handler: Handler
    """A query's `result`, or a command's whole command-response."""
    schema: str | None = None
    """The contract schema for the whole request, beyond its envelope."""


@dataclass
class Server:
    nats: NATS
    service: str
    listener: str
    queries: Mapping[str, Route] = field(default_factory=dict)
    commands: Mapping[str, Route] = field(default_factory=dict)
    authorizer: Authorizer = field(default_factory=LocalOperator)
    _subscriptions: list[Subscription] = field(default_factory=list)

    @property
    def subjects(self) -> list[str]:
        return [f"oe.v1.{self.service}.query.>", f"oe.v1.{self.service}.control.>"]

    async def start(self) -> None:
        for subject in self.subjects:
            self._subscriptions.append(await self.nats.subscribe(subject, cb=self._receive))
            log.info("serving %s", subject)
        await self.nats.flush()

    async def drain(self) -> None:
        """Stop taking requests, and answer the ones already received."""
        for subscription in self._subscriptions:
            await subscription.drain()
        self._subscriptions.clear()

    async def _receive(self, message: Msg) -> None:
        reply = await self.handle(message.subject, message.data, message.headers)
        if message.reply:
            await message.respond(reply)

    async def handle(self, subject: str, data: bytes, headers: Mapping[str, str] | None = None) -> bytes:
        """The encoded reply to one request."""
        kind, _, operation = subject.removeprefix(f"oe.v1.{self.service}.").partition(".")
        body: Any = None
        with receiving(subject, headers, name=f"oe.v1.{self.service}.{kind}.{operation}"):
            try:
                body = envelopes.decode(data)
                envelopes.check(envelopes.COMMAND if kind == "control" else envelopes.QUERY, body)
                request = Request(subject, kind, operation, body, self.listener)
                log.info("%s request", operation, extra=_identity(body))
                self.authorizer.authorize(request)
                route = self._route(subject, kind, operation)
                if route.schema:
                    envelopes.check(route.schema, body)
                result = await route.handler(request)
                reply = result if kind == "control" else _query_reply(body, status="ok", result=result)
            except ServiceError as error:
                log.info("%s rejected: %s", operation, error, extra=_identity(body))
                reply = _error_reply(kind, body, error)
            except Exception:
                log.exception("%s failed", operation, extra=_identity(body))
                reply = _error_reply(kind, body, ServiceError(ErrorCode.INTERNAL, "unexpected service fault"))
        return envelopes.encode(reply)

    def _route(self, subject: str, kind: str, operation: str) -> Route:
        route = (self.commands if kind == "control" else self.queries).get(operation)
        if route is None:
            raise ServiceError(
                ErrorCode.UNSUPPORTED_CAPABILITY, f"{self.service} does not serve {subject}",
                details={"capability": f"{kind}.{operation}"},
            )
        return route


def _identity(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    return {key: body[key] for key in ("request_id", "command_id", "causal_path") if key in body}


def _query_reply(body: Any, **fields: Any) -> dict[str, Any]:
    request_id = body.get("request_id") if isinstance(body, dict) else None
    return ({"request_id": request_id} if _is_token(request_id) else {}) | fields


def _error_reply(kind: str, body: Any, error: ServiceError) -> dict[str, Any]:
    if kind != "control":
        return _query_reply(body, status="error", error=error.to_dict())
    command_id = body.get("command_id") if isinstance(body, dict) else None
    # A command-response must name a command; a body too broken to have one gets a placeholder.
    return {"command_id": command_id if _is_token(command_id) else "unknown", "status": "rejected", "error": error.to_dict()}


def _is_token(value: Any) -> bool:
    return isinstance(value, str) and not envelopes.violations("common/v1/types.schema.json#/$defs/token", value)
