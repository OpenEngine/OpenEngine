"""Typed errors for `common/v1/error.schema.json`.

A reply's `retryable` is authoritative over the default its code implies, so
it is kept as sent. An unknown code is kept as its string.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_STATE = "INVALID_STATE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    COMMAND_ID_CONFLICT = "COMMAND_ID_CONFLICT"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    UNAVAILABLE = "UNAVAILABLE"
    CAUSAL_PATH_TOO_LONG = "CAUSAL_PATH_TOO_LONG"
    STALE_APPROVAL = "STALE_APPROVAL"
    INVALID_CURSOR = "INVALID_CURSOR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INTERNAL = "INTERNAL"


# Only UNAVAILABLE is retryable by definition; the rest default to not.
RETRYABLE_BY_DEFAULT = frozenset({ErrorCode.UNAVAILABLE})


class ServiceError(Exception):
    """An error a service replied with, or one the client met on its behalf."""

    def __init__(
        self, code: ErrorCode | str, message: str, *, retryable: bool | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = ErrorCode(code) if code in ErrorCode.__members__ else code
        self.message = message
        self.retryable = self.code in RETRYABLE_BY_DEFAULT if retryable is None else retryable
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": str(self.code), "message": self.message, "retryable": self.retryable}
        if self.details is not None:
            error["details"] = self.details
        return error

    @classmethod
    def from_dict(cls, error: dict[str, Any]) -> ServiceError:
        return cls(error["code"], error["message"], retryable=error["retryable"], details=error.get("details"))


def invalid_argument(violations: list[tuple[str, str]]) -> ServiceError:
    return ServiceError(
        ErrorCode.INVALID_ARGUMENT,
        "; ".join(f"{field}: {description}" for field, description in violations),
        details={"violations": [{"field": field, "description": description} for field, description in violations]},
    )


class Unavailable(ServiceError):
    """No service answered: nothing subscribed, or no reply in time.

    After a timeout a command's outcome is unknown; retry it with the same command ID.
    """

    def __init__(self, subject: str, reason: str, *, no_responders: bool = False) -> None:
        super().__init__(ErrorCode.UNAVAILABLE, f"{subject}: {reason}")
        self.subject = subject
        self.no_responders = no_responders
