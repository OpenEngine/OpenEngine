"""Worktree-shaped and VM-shaped `WorkspaceProviderPort` doubles, and a
host-shaped `AllocationProvider` double for the managed lifecycle.

Neither touches a filesystem, a VM CLI or any other host resource -- they are
"shaped" like the real #1054 and #1114 adapters only in what they track
(a path versus a VM id) and when they refuse to recover. Both are exercised by
the same conformance tests in `test_provider_conformance.py`, which is what
proves the application layer never needs to know which one it holds.

Both key their ownership set by `locator_key`, so a test can simulate "this
process restarted" by constructing a new double and seeding its `owned` set
from the one that ran before, regardless of which concrete class it is.
"""

from __future__ import annotations

import asyncio
import uuid

from openengine.messaging import ServiceError

from virtual_control_plane.application.ports import (
    Ownership, OwnershipMismatch, Provisioned, ProviderUnavailable, RuntimeContext,
)
from virtual_control_plane.persistence.store import Locator, WorkspaceProvider

MISSING_TOOL = "oe-missing-tool"
"""A tool the doubles advertise but whose "image" lacks it, so preparation fails terminally."""


class _FakeProvider:
    provider: WorkspaceProvider
    locator_key: str
    prefix: str

    def __init__(self) -> None:
        self.owned: set[str] = set()
        self.destroyed: list[Locator] = []

    async def create(self) -> Provisioned:
        value = f"{self.prefix}-{uuid.uuid4()}"
        self.owned.add(value)
        return Provisioned({self.locator_key: value})

    async def recover(self, locator: Locator) -> Provisioned:
        value = locator.get(self.locator_key)
        if value not in self.owned:
            raise ProviderUnavailable(self.provider, f"no {self.locator_key} {value} owned on this host")
        return Provisioned({self.locator_key: value})

    async def destroy(self, locator: Locator) -> None:
        self.owned.discard(locator[self.locator_key])
        self.destroyed.append(locator)


class FakeWorktreeProvider(_FakeProvider):
    """Worktree/host-process-shaped: a workspace is a path that must still exist to recover."""

    provider = WorkspaceProvider.PROCESS
    locator_key = "path"
    prefix = "/work"


class FakeVmProvider(_FakeProvider):
    """smolvm-shaped: a workspace is a VM id that must still be running to recover."""

    provider = WorkspaceProvider.SMOLVM
    locator_key = "vm_id"
    prefix = "vm"


class FakeAllocationProvider:
    """Host-shaped managed double: compute is a dict keyed by the persisted runtime ID.

    Share `allocations` between two instances to simulate this process restarting
    with the same compute still on the host. `foreign` runtime IDs stand for
    compute under our deterministic name that someone else owns; touching one
    would be adoption, so it raises `OwnershipMismatch` and records nothing.
    """

    def __init__(self, provider: str = "process", tools: tuple[str, ...] = ("git", "terminal", MISSING_TOOL),
                 allocations: dict[str, Locator] | None = None) -> None:
        self.capabilities = {"provider": provider, "isolation": "process", "checkpoint": False, "suspend": False,
                             "restore": "none", "approvals_survive_restore": False, "max_resources": {},
                             "tools": list(tools)}
        self.allocations: dict[str, Locator] = {} if allocations is None else allocations
        self.foreign: set[str] = set()
        self.modified_foreign = False
        self.calls = 0
        self.ensures = 0
        self.failure: Exception | None = None
        self.crash = False

    def validate(self, specification: dict) -> None:
        pass

    def _owned(self, ownership: Ownership) -> None:
        if ownership.runtime_id in self.foreign:
            raise OwnershipMismatch(self.capabilities["provider"], ownership.runtime_id)

    async def provision(self, ownership: Ownership, specification: dict) -> Locator:
        self.calls += 1
        self._owned(ownership)
        if self.failure:
            raise self.failure
        if MISSING_TOOL in specification["spec"].get("tools", []):
            raise ServiceError("PROVIDER_ERROR", f"image lacks required tool {MISSING_TOOL!r}", retryable=False)
        self.allocations.setdefault(ownership.runtime_id, {"fake": ownership.runtime_id})
        if self.crash:
            self.crash = False
            raise asyncio.CancelledError()
        return self.allocations[ownership.runtime_id]

    async def ensure(self, ownership: Ownership, locator: Locator, specification: dict) -> Locator:
        self.ensures += 1
        self._owned(ownership)
        if self.failure:
            raise self.failure
        if ownership.runtime_id not in self.allocations:
            raise ServiceError("PROVIDER_ERROR", "owned allocation is gone", retryable=False)
        return self.allocations[ownership.runtime_id]

    def context(self, ownership: Ownership, locator: Locator, specification: dict) -> RuntimeContext:
        return RuntimeContext(root=locator["fake"], working_directory=".")
