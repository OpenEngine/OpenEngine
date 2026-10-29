"""Ports the virtual control plane's application layer depends on."""
from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from virtual_control_plane.persistence.store import Locator, WorkspaceProvider


class Transaction(Protocol):
    async def get(self, collection: str, key: str) -> dict | None: ...
    async def put(self, collection: str, key: str, value: dict) -> None: ...
    async def all(self, collection: str) -> list[dict]: ...


class Storage(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[Transaction]: ...


class Compute(Protocol):
    capabilities: dict
    def validate(self, specification: dict) -> None: ...
    async def provision(self, ownership: dict, specification: dict) -> None:
        """Reconcile this durable ownership key, including an interrupted creation."""
        ...
    async def inspect(self, ownership: dict) -> bool: ...
    async def destroy(self, ownership: dict, retention: dict) -> None: ...


class Authorizer(Protocol):
    async def authorize(self, actor: str | None, subject: str, owner: str | None) -> None: ...


class Publisher(Protocol):
    async def publish(self, event: dict) -> None:
        """Return only after durable broker acknowledgement."""
        ...


class ExecutionContext(Protocol):
    async def prepare(self, workspace_record: dict, runtime: dict, payload: dict) -> dict:
        """Validate runner support and assemble a runtime context without dispatching.

        Called inside acceptance's transaction: must be bounded and have no
        external side effects. Runtime readiness is supplied by the adapter,
        never inferred from a VM being powered on.
        """
        ...


class RuntimeCommandEncoder(Protocol):
    def encode_steer(self, message: dict) -> bytes: ...

    def encode_cancel(self, message: dict) -> bytes: ...

    def encode_execute(self, message: dict) -> bytes:
        """Validate the internal contract and wire size, then return exact UTF-8 bytes."""
        ...


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
    a provisioned workspace is the execution context's job, so a worktree
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
