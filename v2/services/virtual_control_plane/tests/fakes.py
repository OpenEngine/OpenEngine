"""Worktree-shaped and VM-shaped `WorkspaceProviderPort` doubles.

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

import uuid

from virtual_control_plane.application.ports import Provisioned, ProviderUnavailable
from virtual_control_plane.persistence.store import Locator, WorkspaceProvider


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
