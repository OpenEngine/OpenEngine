from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from openengine.persistence import LOCAL_TENANT, Backend, BackendNotImplemented, DatabaseConfig
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
        connections = PERSISTENCE.open(config).scoped(LOCAL_TENANT)
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
        PERSISTENCE.open(DatabaseConfig(backend=Backend.POSTGRES)).scoped(LOCAL_TENANT)


def test_two_tenant_crud_and_global_collision(config: DatabaseConfig) -> None:
    from openengine.persistence import new_tenant

    async def scenario() -> None:
        opened = PERSISTENCE.open(config)
        a, b = opened.scoped(LOCAL_TENANT), opened.scoped(new_tenant())
        try:
            first = await a.create("github", "shared", "first")
            second = await b.create("slack", "shared", "second")
            assert await b.get(first.id) is None
            assert await a.get(second.id) is None
            assert await b.find("github", "shared") is None
            assert await a.list() == [first]
            assert await b.list() == [second]
            await b.rename(first.id, "stolen")
            await b.delete(first.id)
            assert await a.get(first.id) == first
            with pytest.raises(IntegrityError):
                await b.create("github", "shared", "collision")
            assert await b.list() == [second]
            await a.rename(first.id, "renamed")
            assert (await a.get(first.id)).name == "renamed"
            await a.delete(first.id)
            assert await a.list() == []
            assert await b.create("github", "shared", "replacement")
        finally:
            await opened.close()

    asyncio.run(scenario())


def test_atomic_route_creation(config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    from connection_bus.persistence import sqlite
    from connection_bus.persistence.routing import resolve_webhook
    from openengine.persistence import new_id

    async def scenario() -> None:
        opened = PERSISTENCE.open(config)
        store = opened.scoped(LOCAL_TENANT)
        fixed_id = new_id()
        monkeypatch.setattr(sqlite, "new_id", lambda: fixed_id)
        try:
            first = await store.create("github", "one", "first")
            with pytest.raises(IntegrityError):
                await store.create("github", "two", "duplicate id")
            assert await resolve_webhook("github", "two", config) is None
            assert await store.list() == [first]
            monkeypatch.undo()
            assert await store.create("github", "two", "succeeds")
        finally:
            await opened.close()

    asyncio.run(scenario())


def test_route_first_webhook_resolution(config: DatabaseConfig) -> None:
    from openengine.persistence import new_tenant
    from connection_bus.persistence.routing import resolve_webhook
    from connection_bus.persistence.schema import connection_routes

    async def scenario() -> None:
        opened = PERSISTENCE.open(config)
        database = PERSISTENCE.database(config)
        other = new_tenant()
        try:
            first = await opened.scoped(LOCAL_TENANT).create("github", "one", "first")
            second = await opened.scoped(other).create("slack", "one", "second")
            assert await resolve_webhook("github", "one", config) == first
            assert await resolve_webhook("slack", "one", config) == second
            assert await resolve_webhook("github", "missing", config) is None
            async with database.transaction() as tx:
                await tx.execute(connection_routes.update().where(
                    connection_routes.c.provider == "github",
                ).values(tenant_id=other))
            assert await resolve_webhook("github", "one", config) is None
            async with database.transaction() as tx:
                await tx.execute(connection_routes.delete())
            assert await resolve_webhook("slack", "one", config) is None
        finally:
            await database.close()
            await opened.close()

    asyncio.run(scenario())


def test_migration_backfills_and_preserves_legacy_rows(tmp_path: Path) -> None:
    import sqlalchemy as sa
    from datetime import UTC, datetime
    from connection_bus.persistence.routing import resolve_webhook
    from connection_bus.persistence.store import Connection

    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config, "0a34e647834f")
    expected = Connection("legacy", "github", "old", "name", datetime(2026, 1, 1, tzinfo=UTC))
    engine = PERSISTENCE.database(config).migration_engine()
    try:
        old = sa.Table("connections", sa.MetaData(), autoload_with=engine)
        with engine.begin() as tx:
            tx.execute(old.insert().values(**vars(expected)))
        PERSISTENCE.upgrade(config)
        assert PERSISTENCE.drift(metadata, config) == []
        assert asyncio.run(resolve_webhook("github", "old", config)) == expected
        downgrade(PERSISTENCE.migrations, PERSISTENCE.database(config), "0a34e647834f")
        with engine.connect() as tx:
            assert tx.execute(old.select()).one().id == expected.id
        PERSISTENCE.upgrade(config)
        assert asyncio.run(resolve_webhook("github", "old", config)) == expected
    finally:
        engine.dispose()


def test_schema_and_route_tenant_validation(config: DatabaseConfig) -> None:
    import uuid
    from sqlalchemy.exc import StatementError
    from openengine.persistence import InvalidTenant, assert_tenant_scoped
    from connection_bus.persistence.schema import connection_routes

    assert assert_tenant_scoped(metadata, "cxn") == []
    for invalid in (uuid.UUID(int=0), str(LOCAL_TENANT), None):
        with pytest.raises(InvalidTenant):
            PERSISTENCE.open(config).scoped(invalid)

    async def scenario() -> None:
        database = PERSISTENCE.database(config)
        try:
            for invalid in (uuid.UUID(int=0), str(LOCAL_TENANT)):
                with pytest.raises(StatementError):
                    async with database.transaction() as tx:
                        await tx.execute(connection_routes.insert().values(
                            provider="github", external_id="bad", tenant_id=invalid,
                        ))
            with pytest.raises(IntegrityError):
                async with database.transaction() as tx:
                    await tx.execute(connection_routes.insert().values(
                        provider="github", external_id="bad", tenant_id=None,
                    ))
        finally:
            await database.close()

    asyncio.run(scenario())
