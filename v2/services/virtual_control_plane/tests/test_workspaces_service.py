"""Explicit-failure paths `test_provider_conformance.py` does not cover:
unsupported and unavailable providers never fall back to another one."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openengine.persistence import LOCAL_TENANT, DatabaseConfig

from virtual_control_plane.application.ports import ProviderUnavailable, UnsupportedProvider
from virtual_control_plane.application.workspaces import WorkspaceService
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.store import WorkspaceProvider
from virtual_control_plane.tests.fakes import FakeVmProvider, FakeWorktreeProvider


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


def test_creating_an_unregistered_provider_fails_explicitly(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        workspaces = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        service = WorkspaceService(workspaces, {})
        try:
            with pytest.raises(UnsupportedProvider):
                await service.create(WorkspaceProvider.SMOLVM)

            assert await workspaces.list() == []
        finally:
            await workspaces.close()

    asyncio.run(scenario())


def test_recovering_with_only_a_different_provider_registered_fails_rather_than_switching(
    config: DatabaseConfig,
) -> None:
    async def scenario() -> None:
        worktree = FakeWorktreeProvider()
        workspaces = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            created = await WorkspaceService(workspaces, {worktree.provider: worktree}).create(worktree.provider)

            # The process restarts with only the smolvm adapter configured -- the
            # worktree-owned workspace must fail loudly, not run as a VM instead.
            vm = FakeVmProvider()
            service = WorkspaceService(workspaces, {vm.provider: vm})

            with pytest.raises(UnsupportedProvider):
                await service.recover(created.id)

            unchanged = await workspaces.get(created.id)
            assert unchanged is not None and unchanged.provider == worktree.provider
        finally:
            await workspaces.close()

    asyncio.run(scenario())


def test_recovering_an_unavailable_provider_fails_rather_than_reducing_isolation(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        vm = FakeVmProvider()
        workspaces = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            created = await WorkspaceService(workspaces, {vm.provider: vm}).create(vm.provider)

            # Simulate a restart where this host no longer owns that VM (it died,
            # or this host never ran it) -- the adapter itself refuses recovery.
            unavailable = FakeVmProvider()
            service = WorkspaceService(workspaces, {unavailable.provider: unavailable})

            with pytest.raises(ProviderUnavailable):
                await service.recover(created.id)

            unchanged = await workspaces.get(created.id)
            assert unchanged is not None and unchanged.locator == created.locator
        finally:
            await workspaces.close()

    asyncio.run(scenario())


def test_destroying_an_unregistered_provider_fails_explicitly(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        worktree = FakeWorktreeProvider()
        workspaces = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            created = await WorkspaceService(workspaces, {worktree.provider: worktree}).create(worktree.provider)

            with pytest.raises(UnsupportedProvider):
                await WorkspaceService(workspaces, {}).destroy(created.id)
        finally:
            await workspaces.close()

    asyncio.run(scenario())
