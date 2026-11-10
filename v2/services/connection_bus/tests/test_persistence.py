from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from openengine.persistence import Backend, BackendNotImplemented, DatabaseConfig
from openengine.persistence.migrations import downgrade

from connection_bus.persistence import PERSISTENCE
from connection_bus.persistence.schema import metadata


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


def test_finds_the_connection_a_webhook_belongs_to(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        connections = PERSISTENCE.open(config)
        try:
            github = await connections.create("github", "4021", "acme")
            await connections.create("slack", "4021", "acme")

            assert await connections.find("github", "4021") == github
            assert await connections.get(github.id) == github
            assert await connections.find("github", "missing") is None
            assert [connection.provider for connection in await connections.list()] == ["github", "slack"]
            with pytest.raises(IntegrityError):
                await connections.create("github", "4021", "again")
        finally:
            await connections.close()

    asyncio.run(scenario())


def test_postgres_store_is_a_stub() -> None:
    with pytest.raises(BackendNotImplemented, match="connection bus's store"):
        PERSISTENCE.open(DatabaseConfig(backend=Backend.POSTGRES))
