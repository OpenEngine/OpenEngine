from __future__ import annotations

import asyncio
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import inspect

from openengine.persistence import LOCAL_TENANT, Backend, BackendNotImplemented, DatabaseConfig
from openengine.persistence.migrations import downgrade

from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.schema import metadata
from virtual_control_plane.persistence.store import WorkspaceProvider, WorkspaceStatus


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


def test_migrations_match_the_schema(config: DatabaseConfig) -> None:
    assert PERSISTENCE.drift(metadata, config) == []


def test_migrations_roll_back(config: DatabaseConfig) -> None:
    database = PERSISTENCE.database(config)
    downgrade(PERSISTENCE.migrations, database, "base")

    assert inspect(database.migration_engine()).get_table_names() == ["alembic_version"]


def test_tracks_a_workspace_through_its_lifecycle(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        workspaces = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            workspace = await workspaces.create(WorkspaceProvider.PROCESS, {"path": "/work/ws-1"})
            other = await workspaces.create(WorkspaceProvider.SMOLVM)
            ready = await workspaces.set_status(workspace.id, WorkspaceStatus.READY)

            assert workspace.status is WorkspaceStatus.PROVISIONING
            assert workspace.locator == {"path": "/work/ws-1"}
            assert other.locator is None
            assert ready is not None and ready.status is WorkspaceStatus.READY
            assert ready.updated_at >= ready.created_at and ready.updated_at.tzinfo is UTC
            assert await workspaces.get(workspace.id) == ready
            assert await workspaces.list(WorkspaceStatus.READY) == [ready]
            assert await workspaces.list() == [ready, other]
            assert await workspaces.set_status("missing", WorkspaceStatus.STOPPED) is None
        finally:
            await workspaces.close()

    asyncio.run(scenario())


def test_recovering_ownership_updates_the_locator(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        workspaces = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
        try:
            workspace = await workspaces.create(WorkspaceProvider.SMOLVM, {"vm_id": "vm-1"})

            recovered = await workspaces.set_locator(workspace.id, {"vm_id": "vm-1", "host": "node-2"})

            assert recovered is not None and recovered.locator == {"vm_id": "vm-1", "host": "node-2"}
            assert recovered.updated_at >= workspace.updated_at
            assert await workspaces.set_locator("missing", {}) is None
        finally:
            await workspaces.close()

    asyncio.run(scenario())


def test_postgres_store_is_a_stub() -> None:
    with pytest.raises(BackendNotImplemented, match="virtual control plane's store"):
        PERSISTENCE.open(DatabaseConfig(backend=Backend.POSTGRES)).scoped(LOCAL_TENANT)
