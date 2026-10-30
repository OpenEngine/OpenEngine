"""Caller identity from a broker-enforced subject namespace.

This adapter is safe only with broker permissions that bind each credential to
its own request namespace. Headers and payload fields never establish identity.
The broker, its configuration, service credentials and in-process code are trusted.
Managed provisioning and broker endpoint verification are separate requirements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from openengine.messaging.errors import ErrorCode, ServiceError

_TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z")


class InvalidCallerRoute(ValueError):
    def __init__(self) -> None:
        super().__init__("a caller route must use literal tokens and name an oe.v1 query or control operation")


class CallerAuthorizerRequired(ValueError):
    def __init__(self) -> None:
        super().__init__("caller ingress requires an explicit resource authorizer")


def _token(value: str) -> str:
    if not _TOKEN.fullmatch(value):
        raise InvalidCallerRoute()
    return value


@dataclass(frozen=True)
class Principal:
    """Stable caller ID, established by the trusted ingress rather than the body."""

    id: str

    def __post_init__(self) -> None:
        _token(self.id)


def reply_prefix(principal: Principal) -> str:
    """Pass this as nats-py's inbox_prefix when connecting the caller."""
    return f"_INBOX.oe.{principal.id}"


def caller_subject(principal: Principal, subject: str) -> str:
    """Wrap an existing query/control subject without changing the envelope."""
    parts = subject.split(".")
    if len(parts) < 5 or parts[:2] != ["oe", "v1"] or parts[3] not in {"query", "control"}:
        raise InvalidCallerRoute()
    for part in parts[2:]:
        _token(part)
    return f"oe.v1.caller.{principal.id}." + ".".join(parts[2:])


@dataclass(frozen=True)
class CallerIngress:
    """One admission snapshot; replace it to withdraw a principal before admission.

    Broker configuration must also revoke the credential and existing connections.
    Withdrawing admission does not undo a handler that has already started.
    """

    principals: frozenset[Principal]

    def subjects(self, service: str) -> list[str]:
        _token(service)
        return [f"oe.v1.caller.*.{service}.{kind}.>" for kind in ("query", "control")]

    def admit(self, principal: Principal | None) -> None:
        if principal not in self.principals:
            raise _denied()

    def verify(self, service: str, subject: str, reply: str) -> tuple[Principal, str]:
        """Reject before any handler or reply, including malformed-request errors."""
        parts = subject.split(".")
        if len(parts) < 7 or parts[:3] != ["oe", "v1", "caller"] or parts[4] != service:
            raise _denied()
        if any(not _TOKEN.fullmatch(part) for part in parts[3:]) or parts[5] not in {"query", "control"}:
            raise _denied()
        principal = Principal(parts[3])
        self.admit(principal)
        prefix = reply_prefix(principal) + "."
        if not reply.startswith(prefix) or any(not _TOKEN.fullmatch(part) for part in reply[len(prefix):].split(".")):
            raise _denied()
        return principal, "oe.v1." + ".".join(parts[4:])


def _denied() -> ServiceError:
    return ServiceError(ErrorCode.PERMISSION_DENIED, "caller route or reply is not authorized")
