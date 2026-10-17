"""Invocation data supplied to reusable providers, never persisted as a session."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class ACPConnectContext:
    """Engine-independent connection context; applications may extend this type.

    Providers must not retain this on their shared configuration objects.
    Deployment-specific sandbox specifications belong in a typed extension.
    """

    workspace: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    execution_id: str | None = None


__all__ = ["ACPConnectContext"]
