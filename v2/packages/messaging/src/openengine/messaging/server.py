"""Serve a service's query and control subjects over NATS request/reply.

The adapter subscribes to `oe.v1.{service}.query.>` and
`oe.v1.{service}.control.>`, and for each request:

1. decodes it and checks its envelope (and the route's own schema, if it
   names one), replying INVALID_ARGUMENT when either is broken. A command
   route that keeps receipts checks its envelope itself, once the request
   has a well-formed `command_id`, so it can record rejecting it;
2. asks the Authorizer, replying PERMISSION_DENIED when it refuses;
3. runs the route's handler, replying with its result or the ServiceError it
   raises. A subject with no route is UNSUPPORTED_CAPABILITY, and any other
   exception INTERNAL.

A route's key may name subject parameters, as in `operations.{operation_id}.get`.
Each must be one token of the type `common/v1/types.schema.json` defines under
its name, or the request is INVALID_ARGUMENT naming `subject.{name}`; the
handler reads them from `Request.parameters`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription
from nats.errors import Error as NatsError

from openengine.messaging import envelopes
from openengine.messaging.errors import ErrorCode, ServiceError, invalid_argument
from openengine.telemetry import receiving

TYPES = "common/v1/types.schema.json"

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
    parameters: Mapping[str, str] = field(default_factory=dict)
    """The subject's parameters, by the names its route's key gives them."""


class Authorizer(Protocol):
    def authorize(self, request: Request) -> None:
        """Return if the request may proceed; raise a PERMISSION_DENIED ServiceError if not."""

    def authorize_project(self, request: Request, project: str) -> None:
        """Return if the request's actor may act on `project`'s resources; raise a PERMISSION_DENIED ServiceError if not.

        Handlers call it with the project a request names or the resource it reaches
        belongs to, after `authorize` has allowed the request. The project name comes
        from the payload, so it says where to act, never who may: only the actor's
        verified identity can grant that.
        """


class LocalOperator:
    """Loopback-only local-operator mode, the one `oe up` runs.

    NATS has no accounts here: the local nats-server listens only on loopback,
    so whoever reaches it is the operator on this machine. A request is
    allowed only when the server it arrived through listens on loopback alone,
    as `listen_address` reports it; a server bound to any other address, such
    as 0.0.0.0, is reachable from off the machine, so its requests are
    PERMISSION_DENIED until a non-loopback authorizer exists. The local
    operator may act on every project.
    """

    def authorize(self, request: Request) -> None:
        if not is_loopback(urlsplit(request.listener).hostname):
            raise ServiceError(
                ErrorCode.PERMISSION_DENIED,
                f"local-operator mode serves loopback requests only, not ones through {request.listener}",
                details={"permission": f"{request.kind}.{request.operation}"},
            )

    def authorize_project(self, request: Request, project: str) -> None:
        self.authorize(request)


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
    receipts: bool = False
    """The handler checks a command's envelope itself, given only that its `command_id` is well formed."""


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

    async def close(self, timeout: float = 5.0) -> None:
        """Drain replies, then close this server's dedicated NATS connection.

        A broker outage must not prevent shutdown. Bound the entire drain
        below the supervisor's stop deadline, and close even if the broker
        disappears during it. Callers still own their other resources.
        """
        try:
            if self.nats.is_connected:
                await asyncio.wait_for(self._drain_connection(), timeout=timeout)
        except (TimeoutError, NatsError):
            log.warning("NATS drain incomplete; closing %s", self.service, exc_info=True)
        finally:
            await self.nats.close()
            self._subscriptions.clear()

    async def _drain_connection(self) -> None:
        await self.drain()
        # All owned subscriptions have drained; flush their final replies.
        await self.nats.flush()

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
                if not self._keeps_receipt(kind, operation, body):
                    envelopes.check(envelopes.COMMAND if kind == "control" else envelopes.QUERY, body)
                # A reconnect may reach a differently exposed broker. Never
                # authorize using the listening address captured at startup.
                # The explicit listener remains useful for transport-free tests.
                listener = self.listener
                if self.nats is not None:
                    listener = (
                        listen_address(self.nats)
                        if self.nats.is_connected or self.nats.is_draining else ""
                    )
                request = Request(subject, kind, operation, body, listener)
                log.info("%s request", operation, extra=_identity(body))
                self.authorizer.authorize(request)
                route, parameters = self._route(subject, kind, operation)
                request = dataclasses.replace(request, parameters=parameters)
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

    def _keeps_receipt(self, kind: str, operation: str, body: Any) -> bool:
        """Whether a receipt-keeping route takes this command, its `command_id` being one to record."""
        if kind != "control" or not isinstance(body, dict) or "command_id" not in body:
            return False
        if envelopes.violations(f"{TYPES}#/$defs/command_id", body["command_id"]):
            return False
        return any(route.receipts and _match(key, operation) is not None for key, route in self.commands.items())

    def _route(self, subject: str, kind: str, operation: str) -> tuple[Route, dict[str, str]]:
        for key, route in (self.commands if kind == "control" else self.queries).items():
            if (parameters := _match(key, operation)) is not None:
                found = [
                    (f"subject.{name}", description)
                    for name, value in parameters.items()
                    for _, description in envelopes.violations(f"{TYPES}#/$defs/{name}", value)
                ]
                if found:
                    raise invalid_argument(found)
                return route, parameters
        raise ServiceError(
            ErrorCode.UNSUPPORTED_CAPABILITY, f"{self.service} does not serve {subject}",
            details={"capability": f"{kind}.{operation}"},
        )


def _match(key: str, operation: str) -> dict[str, str] | None:
    """The parameters `operation` gives `key`'s `{name}` tokens, or None when it does not fit `key`."""
    names, tokens = key.split("."), operation.split(".")
    if len(names) != len(tokens):
        return None
    parameters = {}
    for name, token in zip(names, tokens):
        if name.startswith("{") and name.endswith("}"):
            parameters[name[1:-1]] = token
        elif name != token:
            return None
    return parameters


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
