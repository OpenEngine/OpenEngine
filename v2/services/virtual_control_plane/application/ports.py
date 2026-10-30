"""Ports the virtual control plane's application layer depends on.

A port is implemented twice for this slice's acceptance criteria -- once by a
Git worktree/host-process-shaped double, once by a smolvm-shaped double -- so
`tests/test_provider_conformance.py` can run the same lifecycle assertions
against both without the application layer branching on which one it holds.
`providers/smolvm` is the real managed adapter behind `AllocationProvider`;
#1054 adds the worktree one and #1055/#1118 the runtime-supervision side. Transactional persistence is defined in `application/transactions.py`;
`application/outbox.py` publishes the events those transactions append.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from virtual_control_plane.application.transactions import UnitOfWork as UnitOfWork
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


class EventPublisher(Protocol):
    """Publishes one public event envelope, returning only once the broker acknowledges it.

    Raise if the acknowledgement does not arrive. `OutboxPublisher` calls this
    with committed events and retries the identical envelope, so an
    implementation must use `event_id` as the broker's deduplication ID.
    """

    async def publish(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Ownership:
    """The durable identity a managed provider labels its compute with.

    Persisted at acceptance, before any provider call, so a crash between a
    provider effect and its commit repeats the same identity on restart. A
    provider derives deterministic names from it and must refuse to adopt or
    alter compute carrying a different owner.
    """

    workspace_id: str
    runtime_id: str
    generation: int


@dataclass(frozen=True)
class RuntimeContext:
    """Where work happens inside a provisioned runtime, as its provider lays it out.

    Command acceptance and delivery read these instead of hard-coding guest
    paths: a worktree's root is a host directory, a VM's is a guest path.
    """

    root: str
    """The workspace root; context files and instructions live beneath it."""
    working_directory: str
    """Where an agent starts, relative to `root`."""
    instruction_files: tuple[str, ...] = ()
    """Standing-instruction files beneath `root`, relative to it."""


class OwnershipMismatch(RuntimeError):
    """Compute under this workspace's deterministic name belongs to someone else.

    Terminal: the foreign compute is left untouched and the operation fails.
    """

    def __init__(self, provider: str, name: str) -> None:
        super().__init__(f"{provider} compute {name!r} is owned by another workspace or runtime")
        self.provider, self.name = provider, name


class AllocationProvider(Protocol):
    """Managed allocation boundary; implementations must honor the identity after restart.

    Every method receives the persisted `Ownership`. `provision` and `ensure`
    run outside any transaction and may be interrupted at any point; a repeated
    call with the same ownership must converge on the same compute, never a
    second allocation. Returned locators remain opaque to VCP: only the
    provider that produced one reads it back. No executor is implied.

    Failures: raise a retryable `ServiceError` (`UNAVAILABLE`) for transient
    provider trouble -- the lifecycle retries it with capped backoff and
    gives up after a bounded number of attempts; any other exception is
    terminal for the operation. `OwnershipMismatch` is always terminal.
    """

    capabilities: dict[str, Any]

    def validate(self, specification: dict[str, Any]) -> None:
        """Reject provider-specific constraints at acceptance, with a `ServiceError`.

        Pure and bounded: runs inside the acceptance transaction. Whole-vCPU
        or image requirements belong here, not in the shared lifecycle.
        """
        ...

    async def provision(self, ownership: Ownership, specification: dict[str, Any]) -> Locator:
        """Create (or finish creating) the owned compute and prepare it; idempotent."""
        ...

    async def ensure(self, ownership: Ownership, locator: Locator, specification: dict[str, Any]) -> Locator:
        """Reassert ownership of existing compute and make it running and prepared.

        Never reprovisions compute that is gone and never adopts foreign compute.
        """
        ...

    def context(self, ownership: Ownership, locator: Locator, specification: dict[str, Any]) -> RuntimeContext:
        """Describe where work happens in this runtime; no provider call."""
        ...
