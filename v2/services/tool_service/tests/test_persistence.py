from __future__ import annotations

from pathlib import Path

import pytest
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from openengine.persistence.tenancy import LOCAL_TENANT, assert_tenant_scoped, new_tenant

from openengine.persistence import Backend, BackendNotImplemented, DatabaseConfig, UtcDateTime
from openengine.persistence.migrations import downgrade, upgrade, current_revision

from tool_service.persistence import PERSISTENCE
from tool_service.persistence.schema import metadata


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


def test_migrations_match_the_schema(config: DatabaseConfig) -> None:
    assert PERSISTENCE.drift(metadata, config) == []
    assert assert_tenant_scoped(metadata, PERSISTENCE.name) == []


def test_migrations_create_the_tables_in_four_revisions(config: DatabaseConfig) -> None:
    database = PERSISTENCE.database(config)
    engine = database.migration_engine()
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version", "tools", "tool_grants", "tool_invocations", "tool_invocation_attempts", "policies",
    }
    grant_columns = {c["name"] for c in inspect(engine).get_columns("tool_grants")}
    assert "when" in grant_columns and "constraints" not in grant_columns
    downgrade(PERSISTENCE.migrations, database, "01c7cfcaaab7")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version", "tools", "tool_grants", "tool_invocations", "tool_invocation_attempts",
    }


def test_migrations_roll_back(config: DatabaseConfig) -> None:
    database = PERSISTENCE.database(config)
    downgrade(PERSISTENCE.migrations, database, "base")

    assert inspect(database.migration_engine()).get_table_names() == ["alembic_version"]


def test_resources_are_unique_by_namespace_and_name(config: DatabaseConfig) -> None:
    inspector = inspect(PERSISTENCE.database(config).migration_engine())
    for table in ("tools", "tool_grants", "tool_invocations", "policies"):
        assert [c["column_names"] for c in inspector.get_unique_constraints(table)] == [["tenant_id", "namespace", "name"]], table
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["tenant_id", "uid"], table


def test_there_is_no_store_yet() -> None:
    for backend in (Backend.SQLITE, Backend.POSTGRES):
        with pytest.raises(BackendNotImplemented, match="tools's store"):
            PERSISTENCE.open(DatabaseConfig(backend=backend))


TABLES = tuple(metadata.sorted_tables)


def row(table: sa.Table, **overrides: object) -> dict:
    """Populate required fields while leaving optional columns null."""
    values = {}
    for column in table.columns:
        if column.nullable or column.name == 'tenant_id':
            continue
        if isinstance(column.type, sa.Integer):
            values[column.name] = 1
        elif isinstance(column.type, sa.JSON):
            values[column.name] = {}
        elif isinstance(column.type, (sa.DateTime, UtcDateTime)):
            values[column.name] = datetime.now(UTC)
        else:
            values[column.name] = 'same'
    return values | overrides


def test_migrated_tenant_leading_keys(config: DatabaseConfig) -> None:
    engine = PERSISTENCE.database(config).migration_engine()
    inspector = inspect(engine)
    for table in TABLES:
        assert inspector.get_pk_constraint(table.name)['constrained_columns'] == [
            column.name for column in table.primary_key]
        tenant = next(c for c in inspector.get_columns(table.name) if c['name'] == 'tenant_id')
        assert tenant['nullable'] is False
        assert tenant['default'] is None
    fk, = inspector.get_foreign_keys('tool_invocation_attempts')
    assert fk['constrained_columns'] == ['tenant_id', 'invocation_uid']
    assert fk['referred_columns'] == ['tenant_id', 'uid']
    assert fk['options']['ondelete'] == 'CASCADE'
    engine.dispose()


@pytest.mark.parametrize('table', TABLES, ids=lambda table: table.name)
def test_two_tenants_share_names_and_row_ids(config: DatabaseConfig, table: sa.Table) -> None:
    engine = PERSISTENCE.database(config).migration_engine()
    first, second = new_tenant(), new_tenant()
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        if table.name == 'tool_invocation_attempts':
            parent = metadata.tables['tool_invocations']
            for tenant in (first, second):
                connection.execute(parent.insert(), row(parent, tenant_id=tenant))
        for tenant in (first, second):
            connection.execute(table.insert(), row(table, tenant_id=tenant))
        assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 2
        if 'uid' in table.c:
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(table.insert(), row(table, tenant_id=first, name='another'))
        # Different row ID still collides on the resource name within one tenant.
        duplicate = row(table, tenant_id=first)
        if 'uid' in table.c:
            duplicate['uid'] = 'another'
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(table.insert(), duplicate)
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(table.insert(), row(table, tenant_id=None))
    engine.dispose()


def test_attempt_fk_and_cascade_isolation(config: DatabaseConfig) -> None:
    engine = PERSISTENCE.database(config).migration_engine()
    parent = metadata.tables['tool_invocations']
    child = metadata.tables['tool_invocation_attempts']
    first, second = new_tenant(), new_tenant()
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        connection.execute(parent.insert(), row(parent, tenant_id=first, uid='private', name='private'))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(child.insert(), row(child, tenant_id=second, invocation_uid='private'))
        for tenant in (first, second):
            connection.execute(parent.insert(), row(parent, tenant_id=tenant))
            connection.execute(child.insert(), row(child, tenant_id=tenant))
        connection.execute(parent.delete().where(parent.c.tenant_id == first))
        assert connection.execute(sa.select(child.c.tenant_id)).scalars().all() == [second]
    engine.dispose()


def test_populated_upgrade_and_local_downgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    database = PERSISTENCE.database(config)
    original = type(database).migration_engine

    def enforced_engine(self):
        engine = original(self)
        @sa.event.listens_for(engine, 'connect')
        def enable_fk(dbapi_connection, _):
            dbapi_connection.execute('PRAGMA foreign_keys=ON')
        return engine

    monkeypatch.setattr(type(database), 'migration_engine', enforced_engine)
    upgrade(PERSISTENCE.migrations, database, '48be4be506f8')
    engine = database.migration_engine()
    old = sa.MetaData()
    old.reflect(engine)
    before = {}
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in TABLES:
            legacy = old.tables[table.name]
            connection.execute(legacy.insert(), row(legacy))
            before[table.name] = connection.execute(sa.select(legacy)).mappings().all()
    upgrade(PERSISTENCE.migrations, database)
    with engine.connect() as connection:
        for table in TABLES:
            migrated = connection.execute(sa.select(table)).mappings().one()
            assert migrated['tenant_id'] == LOCAL_TENANT
            assert {key: value for key, value in migrated.items() if key != 'tenant_id'} == {
                key: (value.replace(tzinfo=UTC) if isinstance(value, datetime) else value)
                for key, value in before[table.name][0].items()}
    downgrade(PERSISTENCE.migrations, database, '48be4be506f8')
    with engine.connect() as connection:
        for name, rows in before.items():
            assert connection.execute(sa.select(old.tables[name])).mappings().all() == rows
    upgrade(PERSISTENCE.migrations, database)
    assert PERSISTENCE.drift(metadata, config) == []
    engine.dispose()


@pytest.mark.parametrize('table', TABLES, ids=lambda table: table.name)
def test_downgrade_guards_every_table(config: DatabaseConfig, table: sa.Table) -> None:
    database = PERSISTENCE.database(config)
    engine = database.migration_engine()
    with engine.begin() as connection:
        connection.execute(table.insert(), row(table, tenant_id=new_tenant()))
    previous = current_revision(database)
    with pytest.raises(RuntimeError, match='outside LOCAL_TENANT'):
        downgrade(PERSISTENCE.migrations, database, '48be4be506f8')
    assert current_revision(database) == previous
    assert PERSISTENCE.drift(metadata, config) == []
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 1
    engine.dispose()
