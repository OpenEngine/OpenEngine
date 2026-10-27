"""Ports the virtual control plane's application layer depends on.

A port is implemented twice for this slice's acceptance criteria -- once by a
Git worktree/host-process-shaped double, once by a smolvm-shaped double -- so
`tests/test_provider_conformance.py` can run the same lifecycle assertions
against both without the application layer branching on which one it holds.
Real adapters land in #1054 (worktree) and #1114 (smolvm); #1055/#1118 add the
runtime-supervision side. Transactional receipts and an event outbox are
#1111/#1112 -- `UnitOfWork` and `EventPublisher` exist now only so that later
slice does not have to widen `WorkspaceService`'s interface to its callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from virtual_control_plane.persistence.store import Locator, WorkspaceProvider


class ProviderUnavailable(RuntimeError):
    """The provider that owns a workspace cannot act on this host right now.

    Raised instead of silently handing the workspace to a different, available
    provider or running it with reduced isolation.
    """

    def __init__(self, provider: WorkspaceProvider, reason: str) -> None:
        super().__init__(f"{provider.value} is unavailable: {reason}")
        self.provider = provider


class UnsupportedProvider(RuntimeError):
    """No adapter is registered for the requested provider."""

    def __init__(self, provider: WorkspaceProvider) -> None:
        super().__init__(f"no provider adapter is registered for {provider.value}")
        self.provider = provider


@dataclass(frozen=True)
class Provisioned:
    locator: Locator
    """Where the workspace lives and what owns it; opaque outside its provider."""


class WorkspaceProviderPort(Protocol):
    """Provisions, confirms ownership of, and tears down one workspace substrate.

    Storage/provisioning only: starting and fencing the agent that runs inside
    a provisioned workspace is `RuntimeSupervisorPort`'s job, so a worktree
    adapter never needs a VM to exist.
    """

    provider: WorkspaceProvider

    async def create(self) -> Provisioned:
        """Provision a new, exclusively-owned workspace."""
        ...

    async def recover(self, locator: Locator) -> Provisioned:
        """Reassert ownership of a persisted workspace, typically after a restart.

        Raise `ProviderUnavailable` if this host cannot own it right now --
        never fall back to a different provider or to reduced isolation.
        """
        ...

    async def destroy(self, locator: Locator) -> None:
        """Release the workspace. Callers must not still want its data."""
        ...


@dataclass(frozen=True)
class ExecutionContext:
    """What a runtime supervisor needs to start work in a workspace.

    A placeholder shape for #1055/#1118/#1119; nothing in this slice
    implements a `RuntimeSupervisorPort` against it.
    """

    workspace_id: str
    provider: WorkspaceProvider
    locator: Locator


class RuntimeSupervisorPort(Protocol):
    """Starts and fences the agent process/session running inside a provisioned workspace."""

    async def start(self, context: ExecutionContext) -> None: ...

    async def stop(self, context: ExecutionContext) -> None: ...


class UnitOfWork(Protocol):
    """Groups a workspace mutation with the receipts/events it produces.

    This slice has no command-receipt or outbox table yet (#1111, #1112);
    nothing in the application layer currently requires an implementation.
    """

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(self, *exc_info: object) -> bool | None: ...


class EventPublisher(Protocol):
    """Publishes one workspace lifecycle event.

    #1112 gives this a transactional outbox so delivery survives a crash
    between commit and publish; the shared messaging `Client` provides no
    durable deduplication of its own, so until then this is best-effort only.
    """

    async def publish(self, event: dict[str, Any]) -> None: ...
