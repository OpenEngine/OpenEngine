from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import inspect

from openengine.persistence import Backend, BackendNotImplemented, DatabaseConfig
from openengine.persistence.migrations import downgrade

from graph_engine.persistence import PERSISTENCE
from graph_engine.persistence.schema import metadata


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


def test_registering_a_graph_again_adds_a_version(config: DatabaseConfig) -> None:
    async def scenario() -> None:
        graphs = PERSISTENCE.open(config)
        try:
            first = await graphs.register("triage", {"nodes": ["read"]})
            second = await graphs.register("triage", {"nodes": ["read", "label"]})
            other = await graphs.register("deploy", {"nodes": []})

            assert (first.version, second.version, other.version) == (1, 2, 1)
            assert await graphs.latest("triage") == second
            assert await graphs.get(first.id) == first
            assert await graphs.versions("triage") == [first, second]
            assert await graphs.latest("missing") is None
        finally:
            await graphs.close()

    asyncio.run(scenario())


def test_postgres_store_is_a_stub() -> None:
    with pytest.raises(BackendNotImplemented, match="graph engine's store"):
        PERSISTENCE.open(DatabaseConfig(backend=Backend.POSTGRES))
