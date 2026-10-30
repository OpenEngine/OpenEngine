"""Serve a service's query and control subjects over NATS request/reply.

The adapter subscribes to the service's query and control subjects in a queue
group named after the service, so instances of one service share its
requests instead of each answering every one. Which subjects depends on the
`tenancy` mode (see `tenancy`): dedicated serves `oe.v1.{service}.query.>`
and `.control.>` for the one configured tenant; shared serves
`oe.v1.*.{service}.query.>` and `.control.>` in the service account, where
the `*` is the account key NATS wrote in as the request crossed from the
tenant's account. For each request the adapter:

0. works out the tenant: the configured one in dedicated mode; in shared
   mode the account key's tenant through the `AccountResolver`, replying
   PERMISSION_DENIED when the key is malformed or no tenant has it. The
   account token is stripped before routing, so routes and parameters are the
   same in both modes;

1. decodes it and checks its envelope (and the route's own schema, if it
   names one), replying INVALID_ARGUMENT when either is broken. A command
   route that keeps receipts checks its envelope itself, once the request
   has a well-formed `command_id`, so it can record rejecting it;
2. asks the Authorizer, replying PERMISSION_DENIED when it refuses, and
   attaches the `Principal` it returns as `Request.principal`;
3. runs the route's handler, replying with its result or the ServiceError it
   raises. A subject with no route is UNSUPPORTED_CAPABILITY, and any other
   exception INTERNAL.

A route's key may name subject parameters, as in `operations.{operation_id}.get`.
Each must be one token of the type `common/v1/types.schema.json` defines under
its name, or the request is INVALID_ARGUMENT naming `subject.{name}`; the
handler reads them from `Request.parameters`.

Span names follow the subject template, `oe.v1.{account}.{svc}.{kind}.{operation}`
in shared mode, so an account key never names a span.
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
from nats.aio.transport import TcpTransport
from nats.errors import Error as NatsError

from openengine.messaging import envelopes
from openengine.messaging.errors import ErrorCode, ServiceError, invalid_argument
from openengine.messaging.tenancy import (
    ACCOUNT_POSITION,
    LOCAL_PRINCIPAL,
    TYPES,
    Dedicated,
    Mode,
    Principal,
    Shared,
    Tenancy,
    is_account,
    subject_template,
)
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
    parameters: Mapping[str, str] = field(default_factory=dict)
    """The subject's parameters, by the names its route's key gives them."""
    principal: Principal | None = None
    """Whom the request is for, as the server derived it from its tenancy mode and the Authorizer confirmed it.

    The server sets it before asking the Authorizer and again from the Authorizer's answer, so
    a handler always finds one; it is None only on a Request built by hand.
    """


class Authorizer(Protocol):
    def authorize(self, request: Request) -> Principal:
        """The Principal the request acts as, if it may proceed; raise a PERMISSION_DENIED ServiceError if not.

        `request.principal` is the tenant the server derived from its tenancy mode, which an
        authorizer may return as is or refine, but never replace with another tenant's.
        """

    def authorize_project(self, request: Request, project: str) -> None:
        """Return if the request's actor may act on `project`'s resources; raise a PERMISSION_DENIED ServiceError if not.

        Handlers call it with the project a request names or the resource it reaches
        belongs to, after `authorize` has allowed the request. The project name comes
        from the payload, so it says where to act, never who may: only the actor's
        verified identity can grant that.
        """


class LocalOperator:
    """Loopback-only local-operator mode, the one `oe up` runs.

    This development mode trusts the managed broker and local machine. It
    does not identify publishers or support remotely bridged subjects.
    NATS has no accounts here: the local nats-server listens only on loopback,
    so whoever reaches it is the operator on this machine. A request is
    allowed only when the server it arrived through listens on loopback alone,
    as `listen_address` reports it; a server bound to any other address, such
    as 0.0.0.0, is reachable from off the machine, so its requests are
    PERMISSION_DENIED until a non-loopback authorizer exists. The local
    operator may act on every project, and acts as the tenant the server
    derived: LOCAL_TENANT in the dedicated mode `oe up` runs.
    """

    def authorize(self, request: Request) -> Principal:
        if not is_loopback(urlsplit(request.listener).hostname):
            raise ServiceError(
                ErrorCode.PERMISSION_DENIED,
                f"local-operator mode serves loopback requests only, not ones through {request.listener}",
                details={"permission": f"{request.kind}.{request.operation}"},
            )
        return request.principal or LOCAL_PRINCIPAL

    def authorize_project(self, request: Request, project: str) -> None:
        self.authorize(request)


def listen_address(nats: NATS) -> str:
    """The address the connected NATS server listens on, from its INFO.

    A server bound to 0.0.0.0 still answers on 127.0.0.1. This describes its
    client listener, not publisher identity or other ingress paths.
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
    tenancy: Tenancy = field(default_factory=Dedicated)
    _subscriptions: list[Subscription] = field(default_factory=list)

    @property
    def subjects(self) -> list[str]:
        service = f"*.{self.service}" if self.tenancy.mode is Mode.SHARED else self.service
        return [f"oe.v1.{service}.query.>", f"oe.v1.{service}.control.>"]

    @property
    def queue(self) -> str:
        """The queue group every instance of the service joins, so each request reaches one of them."""
        return self.service

    async def start(self) -> None:
        for subject in self.subjects:
            self._subscriptions.append(await self.nats.subscribe(subject, queue=self.queue, cb=self._receive))
            log.info("serving %s in queue %s", subject, self.queue)
        await self.nats.flush()

    async def drain(self) -> None:
        """Stop taking requests, and answer the ones already received."""
        for subscription in self._subscriptions:
            await subscription.drain()
        self._subscriptions.clear()

    async def close(self, timeout: float = 5.0) -> None:
        """Drain and close within one budget; raise if final close cannot finish.

        Reserve one fifth of the budget for close, even when a handler stalls.
        The owner must stop its event loop after a failed close so remaining
        callbacks finish cancellation before it releases owned resources.
        """
        closing = asyncio.create_task(self._close_connection(timeout * 0.8))
        try:
            # Client.close() can suppress cancellation while reconnecting.
            # Observe its deadline without waiting for cancellation to finish.
            done, _ = await asyncio.wait([closing], timeout=timeout)
            if not done:
                raise TimeoutError("nats-close-timeout")  # noqa: TRY301 -- share abort cleanup with close errors
            await closing
        except BaseException:
            closing.cancel()
            closing.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            # nats-py has no public abort. A TCP close may wait for buffered
            # bytes, so discard them on failure instead of waiting again.
            transport = self.nats._transport
            if isinstance(transport, TcpTransport) and transport._io_writer is not None:
                transport._io_writer.transport.abort()
            elif transport is not None:
                transport.close()
            raise
        finally:
            self._subscriptions.clear()

    async def _close_connection(self, drain_timeout: float) -> None:
        try:
            if self.nats.is_connected:
                await asyncio.wait_for(self._drain_connection(), timeout=drain_timeout)
        except (TimeoutError, NatsError):
            log.warning("NATS drain incomplete; closing %s", self.service, exc_info=True)
        await self.nats.close()

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
        account, kind, operation = self._split(subject)
        body: Any = None
        name = f"oe.v1.{self.service}.{kind}.{operation}"
        with receiving(subject, headers, name=subject_template(name) if account is not None else name):
            try:
                # Decode first so a refused request still echoes its request_id or command_id.
                body = envelopes.decode(data)
                principal = await self._principal(account, kind, operation)
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
                request = Request(subject, kind, operation, body, listener, principal=principal)
                log.info("%s request", operation, extra=_identity(body))
                request = dataclasses.replace(request, principal=self.authorizer.authorize(request))
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

    def _split(self, subject: str) -> tuple[str | None, str, str]:
        """The subject's account token (shared mode only), kind and operation."""
        tokens = subject.split(".")
        account = None
        if self.tenancy.mode is Mode.SHARED:
            account = tokens[ACCOUNT_POSITION] if len(tokens) > ACCOUNT_POSITION else ""
            del tokens[ACCOUNT_POSITION]
        kind, _, operation = ".".join(tokens).removeprefix(f"oe.v1.{self.service}.").partition(".")
        return account, kind, operation

    async def _principal(self, account: str | None, kind: str, operation: str) -> Principal:
        """The tenant the request belongs to, from the tenancy mode; PERMISSION_DENIED when shared mode cannot tell."""
        if isinstance(self.tenancy, Dedicated):
            return Principal(self.tenancy.tenant_id)
        assert isinstance(self.tenancy, Shared) and account is not None
        tenant_id = await self.tenancy.resolver.resolve(account) if is_account(account) else None
        if tenant_id is None:
            raise ServiceError(
                ErrorCode.PERMISSION_DENIED,
                "the request's account does not belong to any tenant",
                details={"permission": f"{kind}.{operation}"},
            )
        return Principal(tenant_id, account)

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
