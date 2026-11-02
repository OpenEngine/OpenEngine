"""How every managed adapter maps a failure onto the lifecycle's retry policy."""

from __future__ import annotations

from typing import Any, NoReturn

from openengine.messaging import ErrorCode, ServiceError


def unavailable(message: str, **details: Any) -> NoReturn:
    """Transient: the lifecycle retries the same ownership with capped backoff."""
    raise ServiceError(ErrorCode.UNAVAILABLE, message, retryable=True, details=details or None)


def terminal(message: str, **details: Any) -> NoReturn:
    """Final for this operation; ownership is retained for explicit cleanup."""
    raise ServiceError(ErrorCode.PROVIDER_ERROR, message, retryable=False, details=details or None)
