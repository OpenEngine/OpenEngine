"""Live shared-layer tests. Only this fixture bootstraps roles and isolated schemas."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from psycopg.pq import TransactionStatus
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from openengine.persistence import (
    LOCAL_TENANT, Backend, ConfigError, DatabaseConfig, TenantDatabase, TenantId, new_tenant, open_database,
)
from openengine.persistence.postgres import PostgresDatabase


@dataclass
class Cluster:
    config: DatabaseConfig
    schemas: dict[str, str]
    owner: str
    runtime: str


@pytest.fixture(scope="module")
def cluster():
    url = os.environ.get("OE_TEST_POSTGRES_ADMIN_URL")
    if not url:
        pytest.skip("OE_TEST_POSTGRES_ADMIN_URL is not set")
    admin_url = sa.engine.make_url(url).set(drivername="postgresql+psycopg")
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    prefix = "test_" + uuid.uuid4().hex
    owner, runtime, password = prefix + "_owner", prefix + "_runtime", uuid.uuid4().hex
    schemas = {service: prefix + "_" + service for service in ("cxn", "ge", "tools", "vcp")}
    roles, created = [], []
    try:
        with admin.connect() as connection:
            for role in (owner, runtime):
                connection.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'')
                roles.append(role)
            for schema in schemas.values():
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{owner}"')
                created.append(schema)
                connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{schema}" TO "{runtime}"')
                connection.exec_driver_sql(f'CREATE TABLE "{schema}".probe (tenant_id uuid PRIMARY KEY, value text)')
                connection.exec_driver_sql(f'ALTER TABLE "{schema}".probe OWNER TO "{owner}"')
                connection.exec_driver_sql(f'GRANT SELECT, INSERT ON "{schema}".probe TO "{runtime}"')
        config = DatabaseConfig(
            backend=Backend.POSTGRES,
            postgres_url=admin_url.set(username=runtime, password=password).render_as_string(hide_password=False),
            postgres_migration_url=admin_url.set(username=owner, password=password).render_as_string(hide_password=False),
        )
        yield Cluster(config, schemas, owner, runtime)
    finally:
        with admin.connect() as connection:
            for schema in reversed(created):
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            for role in reversed(roles):
                connection.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()


def test_runtime_and_migration_credentials_are_separate(cluster):
    database = open_database(cluster.schemas["cxn"], cluster.config)
    owner = database.migration_engine()
    try:
        with owner.connect() as connection:
            assert connection.scalar(sa.text("SELECT current_user")) == cluster.owner
    finally:
        owner.dispose()

    async def scenario():
        runtime_only = open_database(cluster.schemas["cxn"], DatabaseConfig(
            backend=Backend.POSTGRES, postgres_url=cluster.config.postgres_url))
        try:
            async with runtime_only.transaction(LOCAL_TENANT) as connection:
                assert await connection.scalar(sa.text("SELECT current_user")) == cluster.runtime
                assert not await connection.scalar(sa.text("SELECT has_schema_privilege(current_schema(), 'CREATE')"))
            with pytest.raises(ConfigError, match="OE_POSTGRES_MIGRATION_URL"):
                runtime_only.migration_engine()
        finally:
            await runtime_only.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("service", ["cxn", "ge", "tools", "vcp"])
def test_service_search_path_on_both_engines(cluster, service):
    schema = cluster.schemas[service]
    database = open_database(schema, cluster.config)
    query = sa.text("SELECT current_schema(), current_schemas(false), 'probe'::regclass::oid")
    engine = database.migration_engine()
    try:
        with engine.connect() as connection:
            expected = connection.execute(query).one()
            assert expected[:2] == (schema, [schema])
    finally:
        engine.dispose()

    async def scenario():
        try:
            async with database.transaction(LOCAL_TENANT) as connection:
                assert (await connection.execute(query)).one() == expected
        finally:
            await database.close()
    asyncio.run(scenario())


def test_missing_schema_fails_without_public_fallback_or_runtime_ddl(cluster):
    database = open_database("missing_" + uuid.uuid4().hex, cluster.config)
    engine = database.migration_engine()
    try:
        with pytest.raises(ConfigError, match="must exist"):
            with engine.connect():
                pytest.fail("missing schema accepted")
    finally:
        engine.dispose()

    async def scenario():
        try:
            with pytest.raises(ConfigError, match="must exist"):
                async with database.transaction(LOCAL_TENANT):
                    pytest.fail("missing schema accepted")
        finally:
            await database.close()
    asyncio.run(scenario())


def test_native_tenant_uuid_round_trip(cluster):
    database = open_database(cluster.schemas["cxn"], cluster.config)
    probe = sa.table("probe", sa.column("tenant_id", TenantId()), sa.column("value", sa.String))
    tenant = new_tenant()

    async def scenario():
        try:
            async with TenantDatabase(database, tenant).transaction() as connection:
                await connection.execute(probe.insert().values(tenant_id=tenant, value="uuid"))
            async with TenantDatabase(database, tenant).transaction() as connection:
                row = (await connection.execute(sa.select(probe.c.tenant_id, sa.func.pg_typeof(probe.c.tenant_id))
                                               .where(probe.c.tenant_id == tenant))).one()
                assert row == (tenant, "uuid")
                assert isinstance(row[0], uuid.UUID)
        finally:
            await database.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("exit_mode", ["commit", "rollback", "sql_error", "hook_failure", "cancel_body", "cancel_hook",
                                      "cancel_cleanup", "invalidation"])
def test_hook_ordering_and_single_connection_pool_cleanup(cluster, exit_mode, monkeypatch):
    seen, dictionaries, records, drivers, statements = [], [], [], [], []
    entered, release_cleanup = asyncio.Event(), asyncio.Event()
    original_close = AsyncConnection.close

    async def delayed_close(connection):
        if exit_mode == "cancel_cleanup" and not entered.is_set():
            entered.set()
            await release_cleanup.wait()
        await original_close(connection)

    monkeypatch.setattr(AsyncConnection, "close", delayed_close)
    tenant, next_tenant = new_tenant(), new_tenant()

    class Hooked(PostgresDatabase):
        async def transaction_started(self, connection, tenant):
            assert connection.in_transaction()
            raw = await connection.get_raw_connection()
            assert raw.driver_connection.info.transaction_status == TransactionStatus.INTRANS
            assert connection.info["tenant_id"] == tenant
            dictionaries.append(connection.info)
            seen.append(tenant)
            # Harmless stand-in for a future role hook: no role/bootstrap implementation.
            await connection.execute(sa.text("SELECT set_config('oe.test_tenant', :tenant, true)"), {"tenant": str(tenant)})
            if len(seen) == 1:
                if exit_mode == "hook_failure":
                    raise RuntimeError("hook")
                if exit_mode == "cancel_hook":
                    entered.set()
                    await asyncio.Event().wait()

    database = Hooked(cluster.schemas["ge"], cluster.config.postgres_url)
    database._engine = create_async_engine(database.url(), pool_size=1, max_overflow=0, pool_timeout=2)
    database.configure(database.engine.sync_engine)

    @sa.event.listens_for(database.engine.sync_engine, "checkout")
    def checkout(dbapi, record, proxy):
        assert "tenant_id" not in record.info
        records.append(record)
        drivers.append(dbapi.driver_connection)
        assert dbapi.driver_connection.info.transaction_status == TransactionStatus.IDLE

    @sa.event.listens_for(database.engine.sync_engine, "before_cursor_execute")
    def statement(connection, cursor, sql, parameters, context, executemany):
        statements.append(sql)

    async def scenario():
        async def first():
            async with TenantDatabase(database, tenant).transaction() as connection:
                assert await connection.scalar(sa.text("SELECT current_setting('oe.test_tenant')")) == str(tenant)
                await connection.execute(sa.text("INSERT INTO probe VALUES (:tenant, 'first')"), {"tenant": tenant})
                if exit_mode == "rollback":
                    raise RuntimeError("body")
                if exit_mode == "sql_error":
                    await connection.execute(sa.text("SELECT 1 / 0"))
                if exit_mode == "invalidation":
                    await connection.invalidate()
                    raise RuntimeError("invalidated")
                if exit_mode == "cancel_body":
                    entered.set()
                    await asyncio.Event().wait()
        try:
            task = asyncio.create_task(first())
            if exit_mode.startswith("cancel_"):
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                if exit_mode == "cancel_cleanup":
                    await asyncio.sleep(0)
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                    release_cleanup.set()
            if exit_mode == "commit":
                await task
            else:
                error = (asyncio.CancelledError if exit_mode.startswith("cancel_") else
                         sa.exc.DBAPIError if exit_mode == "sql_error" else RuntimeError)
                with pytest.raises(error):
                    await task
            assert dictionaries and all("tenant_id" not in info for info in dictionaries)
            async with database.engine.connect() as connection:
                assert await connection.scalar(sa.text("SELECT nullif(current_setting('oe.test_tenant', true), '')")) is None
                count = await connection.scalar(sa.text("SELECT count(*) FROM probe WHERE tenant_id = :tenant"),
                                                {"tenant": tenant})
                assert count == int(exit_mode in {"commit", "cancel_cleanup"})
            async with TenantDatabase(database, next_tenant).transaction() as connection:
                assert await connection.scalar(sa.text("SELECT current_setting('oe.test_tenant')")) == str(next_tenant)
            assert seen == [tenant, next_tenant]
            assert all("tenant_id" not in info for info in dictionaries)
            assert all(record is records[0] for record in records)
            if exit_mode == "invalidation":
                assert drivers[0] is not drivers[1]
            else:
                assert all(driver is drivers[0] for driver in drivers)
            assert statements[0] == "SELECT 1"  # psycopg sends BEGIN before this statement
            assert "set_config" in statements[1]
            assert database.engine.pool.checkedout() == 0
        finally:
            await database.close()
    asyncio.run(scenario())
