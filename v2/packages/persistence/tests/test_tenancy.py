from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.schema import CreateIndex, CreateTable

from openengine.persistence import (
    LOCAL_TENANT,
    Backend,
    Database,
    DatabaseConfig,
    DatabaseStore,
    InvalidTenant,
    OpenPersistence,
    ServicePersistence,
    TenantDatabase,
    TenantId,
    Violation,
    assert_tenant_scoped,
    global_table,
    new_id,
    new_tenant,
    parse_tenant,
    tenant_table,
)
from openengine.persistence.tenancy import REGISTRY, MissingReason
from openengine.persistence.tenancy.__main__ import main

NIL = uuid.UUID(int=0)
SERVICES = Path(__file__).parents[3] / "services"


def _schema() -> tuple[sa.MetaData, sa.Table, sa.Table]:
    metadata = sa.MetaData()
    parents = tenant_table(
        "parents", metadata,
        sa.Column("parent_id", sa.String(32), primary_key=True),
        sa.Column("slug", sa.String(32), unique=True),
        sa.Column("name", sa.String(32)),
        sa.UniqueConstraint("name", "slug", name="uq_parents_name_slug"),
        sa.Index("ix_parents_name", "name", unique=True),
    )
    children = tenant_table(
        "children", metadata,
        sa.Column("child_id", sa.String(32), primary_key=True),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("parents.parent_id", ondelete="CASCADE"), nullable=False),
    )
    return metadata, parents, children


def _registry(tmp_path: Path, *entries: tuple[str, str]) -> Path:
    path = tmp_path / "global_tables.toml"
    path.write_text("".join(
        f'[[tables]]\nservice = "{service}"\ntable = "{table}"\nreason = "test"\n' for service, table in entries
    ))
    return path


def _ddl(table: sa.Table) -> str:
    return " ".join(str(CreateTable(table).compile(dialect=sqlite.dialect())).split())


def test_tenant_table_puts_tenant_id_first_in_the_key_uniques_and_foreign_keys() -> None:
    metadata, parents, children = _schema()

    assert [c.name for c in parents.primary_key.columns] == ["tenant_id", "parent_id"]
    assert not parents.c.tenant_id.nullable
    assert isinstance(parents.c.tenant_id.type, TenantId)
    assert "UNIQUE (tenant_id, slug)" in _ddl(parents)
    assert "CONSTRAINT uq_parents_name_slug UNIQUE (tenant_id, name, slug)" in _ddl(parents)
    [index] = parents.indexes
    assert str(CreateIndex(index).compile()) == "CREATE UNIQUE INDEX ix_parents_name ON parents (tenant_id, name)"
    assert ("FOREIGN KEY(tenant_id, parent_id) REFERENCES parents (tenant_id, parent_id) ON DELETE CASCADE"
            in _ddl(children))
    assert len(children.foreign_key_constraints) == 1
    assert assert_tenant_scoped(metadata, "test") == []


def test_a_child_row_cannot_point_at_another_tenants_parent() -> None:
    metadata, parents, children = _schema()
    engine = sa.create_engine("sqlite://")
    sa.event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    metadata.create_all(engine)
    mine, theirs = new_tenant(), new_tenant()

    with engine.begin() as connection:
        connection.execute(parents.insert().values(tenant_id=mine, parent_id="p1"))
        connection.execute(children.insert().values(tenant_id=mine, child_id="c1", parent_id="p1"))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), engine.begin() as connection:
        connection.execute(children.insert().values(tenant_id=theirs, child_id="c2", parent_id="p1"))
    engine.dispose()


def test_reports_tables_that_are_not_tenant_scoped(tmp_path: Path) -> None:
    metadata = sa.MetaData()
    sa.Table("plain", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("parents", metadata, sa.Column("tenant_id", TenantId(), primary_key=True),
             sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(
        "trailing", metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", TenantId(), primary_key=True),
        sa.Column("name", sa.String, unique=True),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("parents.id")),
    )
    sa.Table("untyped", metadata, sa.Column("tenant_id", sa.String, primary_key=True))

    assert [str(v) for v in assert_tenant_scoped(metadata, "test", _registry(tmp_path))] == [
        "plain: no tenant_id column",
        "trailing: tenant_id does not lead the primary key",
        "trailing: tenant_id does not lead unique (name)",
        "trailing: tenant_id does not lead foreign key (parent_id) -> parents",
        "untyped: tenant_id is not a TenantId",
    ]


def test_a_global_table_must_be_registered_and_every_registration_declared(tmp_path: Path) -> None:
    metadata = sa.MetaData()
    routes = global_table("routes", metadata, sa.Column("external_id", sa.String, primary_key=True),
                          reason="maps an inbound event to its tenant")
    tenant_table("events", metadata, sa.Column("event_id", sa.String, primary_key=True),
                 sa.Column("external_id", sa.String, sa.ForeignKey("routes.external_id")))

    assert routes.info["global_table_reason"] == "maps an inbound event to its tenant"
    assert assert_tenant_scoped(metadata, "test", _registry(tmp_path)) == [
        Violation("routes", "global table not registered in global_tables.toml"),
    ]
    assert assert_tenant_scoped(metadata, "test", _registry(tmp_path, ("test", "routes"))) == []
    assert assert_tenant_scoped(metadata, "test", _registry(tmp_path, ("test", "routes"), ("test", "gone"),
                                                            ("other", "elsewhere"))) == [
        Violation("gone", "registered in global_tables.toml but not declared with global_table()"),
    ]
    with pytest.raises(MissingReason):
        global_table("unexplained", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True), reason=" ")


def test_the_registry_starts_empty() -> None:
    assert assert_tenant_scoped(sa.MetaData(), "cxn", REGISTRY) == []


def test_tenant_ids_round_trip_in_canonical_form_on_sqlite_and_refuse_nil() -> None:
    metadata = sa.MetaData()
    rows = sa.Table("rows", metadata, sa.Column("tenant_id", TenantId()))
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)
    tenant = new_tenant()

    with engine.begin() as connection:
        connection.execute(rows.insert().values(tenant_id=tenant))
        assert connection.execute(sa.select(rows.c.tenant_id)).scalar_one() == tenant
        assert connection.execute(sa.text("SELECT tenant_id FROM rows")).scalar_one() == tenant.hex
        with pytest.raises(StatementError, match="nil UUID"):
            connection.execute(rows.insert().values(tenant_id=NIL))
        with pytest.raises(StatementError, match="uuid.UUID"):
            connection.execute(rows.insert().values(tenant_id=str(tenant)))
    engine.dispose()
    assert str(tenant) == str(tenant).lower()
    assert parse_tenant(str(tenant)) == tenant


@pytest.mark.parametrize("text", [
    str(NIL), "0192a4b2-7c3d-7e4f-8a5b-6c7d8e9f0a1b".upper(), LOCAL_TENANT.hex, "not-a-uuid",
])
def test_parsing_accepts_only_a_non_nil_canonical_uuid(text: str) -> None:
    with pytest.raises(InvalidTenant):
        parse_tenant(text)


def test_the_local_tenant_is_fixed_and_not_nil() -> None:
    assert LOCAL_TENANT == uuid.UUID("00000000-0000-7000-8000-000000000001")
    assert LOCAL_TENANT != NIL
    assert parse_tenant(str(LOCAL_TENANT)) == LOCAL_TENANT


def test_new_ids_are_uuid7_and_sort_in_creation_order() -> None:
    ids = []
    for _ in range(3):
        ids += [new_id() for _ in range(500)]
        time.sleep(0.002)

    assert {i.version for i in ids} == {7}
    assert new_tenant().version == 7
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


class Seen(DatabaseStore):
    async def tenants(self) -> list[Any]:
        seen = []
        async with self.database.transaction() as connection:
            seen.append(connection.info["tenant_id"])
        async with self.database.connect() as connection:
            seen.append(connection.info["tenant_id"])
        async with self.database.engine.connect() as connection:
            seen.append(connection.info.get("tenant_id"))
        return seen


def test_scoped_stores_tag_every_transaction_with_their_tenant(tmp_path: Path) -> None:
    persistence = ServicePersistence[Seen]("seen", tmp_path, {Backend.SQLITE: Seen})
    opened = persistence.open(DatabaseConfig(sqlite_directory=tmp_path))
    other = new_tenant()

    async def scenario() -> tuple[list[Any], list[Any]]:
        try:
            return await opened.scoped(LOCAL_TENANT).tenants(), await opened.scoped(other).tenants()
        finally:
            await opened.close()

    mine, theirs = asyncio.run(scenario())

    # The pool hands the same connection back without the last scope's tenant.
    assert mine == [LOCAL_TENANT, LOCAL_TENANT, None]
    assert theirs == [other, other, None]


def test_open_offers_no_store_without_a_tenant(tmp_path: Path) -> None:
    persistence = ServicePersistence[Seen]("seen", tmp_path, {Backend.SQLITE: Seen})
    opened = persistence.open(DatabaseConfig(sqlite_directory=tmp_path))

    assert isinstance(opened, OpenPersistence)
    assert {name for name in dir(opened) if not name.startswith("_")} == {"scoped", "close"}
    assert not hasattr(persistence, "store")
    store = opened.scoped(LOCAL_TENANT)
    assert isinstance(store.database, TenantDatabase)
    assert isinstance(store.database, Database)
    assert store.database.tenant == LOCAL_TENANT
    assert store.database.backend is Backend.SQLITE
    for tenant in (NIL, str(LOCAL_TENANT), None):
        with pytest.raises(InvalidTenant):
            opened.scoped(tenant)


def test_lists_every_services_violations(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--services", str(SERVICES)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "connection_bus: connections: no tenant_id column" in lines
    assert {line.split(":")[0] for line in lines} == {
        "connection_bus", "graph_engine", "tool_service", "virtual_control_plane",
    }


def test_reports_success_when_no_service_has_violations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--services", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "every table is tenant-scoped\n"
