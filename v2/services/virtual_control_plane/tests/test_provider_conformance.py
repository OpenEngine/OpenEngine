"""Shared lifecycle conformance: the same assertions against a worktree-shaped
and a VM-shaped `WorkspaceProviderPort` double.

#1054 (worktree) and #1114 (smolvm) are expected to pass this same module
against their real adapters; `WorkspaceService` never branches on which
provider it holds, so neither should they need a provider-specific test file
for this lifecycle.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openengine.persistence import DatabaseConfig

from virtual_control_plane.application.workspaces import WorkspaceService
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.store import WorkspaceStatus
from virtual_control_plane.tests.fakes import FakeVmProvider, FakeWorktreeProvider

DOUBLES = [FakeWorktreeProvider, FakeVmProvider]


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


@pytest.mark.parametrize("double", DOUBLES, ids=lambda double: double.__name__)
def test_create_persists_the_providers_locator_and_becomes_ready(config: DatabaseConfig, double: type) -> None:
    async def scenario() -> None:
        adapter = double()
        workspaces = PERSISTENCE.open(config)
        service = WorkspaceService(workspaces, {adapter.provider: adapter})
        try:
            workspace = await service.create(adapter.provider)

            assert workspace.status is WorkspaceStatus.READY
            assert workspace.provider == adapter.provider
            assert workspace.locator
            assert await workspaces.get(workspace.id) == workspace
        finally:
            await workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("double", DOUBLES, ids=lambda double: double.__name__)
def test_ownership_survives_a_restart(config: DatabaseConfig, double: type) -> None:
    """A fresh `WorkspaceService`, as this process gets after it restarts, recovers
    the same workspace through the same provider rather than reprovisioning it."""

    async def scenario() -> None:
        first_adapter = double()
        first_workspaces = PERSISTENCE.open(config)
        try:
            workspace = await first_adapter.create()
            created = await first_workspaces.create(first_adapter.provider, workspace.locator)
        finally:
            await first_workspaces.close()

        restarted_adapter = double()
        restarted_adapter.owned = set(first_adapter.owned)
        restarted_workspaces = PERSISTENCE.open(config)
        restarted_service = WorkspaceService(restarted_workspaces, {restarted_adapter.provider: restarted_adapter})
        try:
            recovered = await restarted_service.recover(created.id)

            assert recovered.id == created.id
            assert recovered.provider == first_adapter.provider
            assert recovered.locator == created.locator
        finally:
            await restarted_workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("double", DOUBLES, ids=lambda double: double.__name__)
def test_destroy_releases_the_workspace_through_its_own_provider(config: DatabaseConfig, double: type) -> None:
    async def scenario() -> None:
        adapter = double()
        workspaces = PERSISTENCE.open(config)
        service = WorkspaceService(workspaces, {adapter.provider: adapter})
        try:
            workspace = await service.create(adapter.provider)

            await service.destroy(workspace.id)

            stopped = await workspaces.get(workspace.id)
            assert stopped is not None and stopped.status is WorkspaceStatus.STOPPED
            assert adapter.destroyed == [workspace.locator]
        finally:
            await workspaces.close()

    asyncio.run(scenario())
