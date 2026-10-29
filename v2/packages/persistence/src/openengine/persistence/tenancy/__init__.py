"""Every row belongs to a tenant, and the schema says so.

A service declares its tables with `tenant_table()`, which adds a `tenant_id`
column and puts it first in the primary key, every unique constraint and every
foreign key. A foreign key becomes `(tenant_id, ...) -> parent (tenant_id, ...)`,
so a child row cannot point at another tenant's parent. The few tables that
hold no tenant's data are declared with `global_table(..., reason=...)` and
listed in `global_tables.toml` beside this module, which CODEOWNERS guards.

`assert_tenant_scoped(metadata, service)` lists every way a schema falls short,
and sits beside each service's `drift() == []` test. To see every service's:

    python -m openengine.persistence.tenancy --services services

Stores only see a database through `TenantDatabase`, which
`PERSISTENCE.open(config).scoped(tenant)` builds; every connection it hands out
carries the tenant in `connection.info["tenant_id"]`.
"""

from __future__ import annotations

import tomllib
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Dialect, Engine
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.types import TypeDecorator

from openengine.persistence.config import Backend
from openengine.persistence.database import Database

TENANT_COLUMN = "tenant_id"
GLOBAL_INFO = "global_table_reason"
UNSCOPED_INFO = "unscoped_foreign_key"
REGISTRY = Path(__file__).with_name("global_tables.toml")

# The tenant `oe up` runs everything as. Fixed and published so local data
# keeps its owner across restarts; deliberately not the nil UUID.
LOCAL_TENANT = uuid.UUID("00000000-0000-7000-8000-000000000001")


class InvalidTenant(ValueError):
    pass


class NotATenant(InvalidTenant):
    def __init__(self, value: object) -> None:
        super().__init__(f"a tenant is a uuid.UUID other than the nil UUID, not {value!r}")


class NotCanonical(InvalidTenant):
    def __init__(self, text: str) -> None:
        super().__init__(f"{text!r} is not a UUID in lower-case canonical form")


class MissingReason(ValueError):
    def __init__(self, table: str) -> None:
        super().__init__(f"global table {table!r} needs a reason")


def tenant(value: object) -> uuid.UUID:
    """`value` as a tenant: a UUID other than the nil UUID."""
    if not isinstance(value, uuid.UUID) or value == uuid.UUID(int=0):
        raise NotATenant(value)
    return value


def parse_tenant(text: str) -> uuid.UUID:
    """A tenant from its wire form, the lower-case canonical `xxxxxxxx-xxxx-...`; `str()` writes it."""
    try:
        value = uuid.UUID(text)
    except ValueError:
        raise NotCanonical(text) from None
    if str(value) != text:
        raise NotCanonical(text)
    return tenant(value)


def new_tenant() -> uuid.UUID:
    return uuid.uuid7()


def new_id() -> uuid.UUID:
    """A row ID that sorts by creation, so new rows land at the end of the index."""
    return uuid.uuid7()


class TenantId(TypeDecorator[uuid.UUID]):
    """A tenant's UUID: `uuid` on Postgres, CHAR(32) on SQLite. The nil UUID is refused."""

    impl = sa.Uuid(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        return None if value is None else tenant(value)


def _tenant_column(table: sa.Table) -> sa.Column[Any]:
    return table.c[TENANT_COLUMN]


def _leading(columns: Sequence[str]) -> list[str]:
    return [TENANT_COLUMN, *(name for name in columns if name != TENANT_COLUMN)]


def _scope_primary_key(table: sa.Table) -> None:
    key = table.primary_key
    # Flagged first, so the new key matches the columns marked primary and SQLAlchemy does not warn.
    _tenant_column(table).primary_key = True
    table.append_constraint(sa.PrimaryKeyConstraint(*_leading([c.name for c in key.columns]), name=key.name))


def _scope_uniques(table: sa.Table) -> None:
    for constraint in [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]:
        table.constraints.discard(constraint)
        for column in constraint.columns:
            column.unique = False
        table.append_constraint(sa.UniqueConstraint(
            *_leading([c.name for c in constraint.columns]), name=constraint.name, deferrable=constraint.deferrable,
            initially=constraint.initially, info=constraint.info, **constraint.dialect_kwargs,
        ))
    for index in [i for i in table.indexes if i.unique]:
        table.indexes.discard(index)
        # Expressions and dialect options (a partial index's `*_where`) carry over untouched.
        expressions = [e for e in index.expressions if not (isinstance(e, sa.Column) and e.name == TENANT_COLUMN)]
        sa.Index(index.name, _tenant_column(table), *expressions, unique=True, _table=table, info=index.info,
                 **index.dialect_kwargs)


def _parent(constraint: sa.ForeignKeyConstraint) -> str:
    return constraint.elements[0].target_fullname.rsplit(".", 1)[0]


def _replace_foreign_key(
    table: sa.Table, constraint: sa.ForeignKeyConstraint, local: list[str], remote: list[str], info: dict[str, Any],
) -> None:
    table.constraints.discard(constraint)
    for element in constraint.elements:
        element.parent.foreign_keys.discard(element)
        table.foreign_keys.discard(element)
    table.append_constraint(sa.ForeignKeyConstraint(
        local, remote, name=constraint.name, onupdate=constraint.onupdate, ondelete=constraint.ondelete,
        deferrable=constraint.deferrable, initially=constraint.initially, use_alter=constraint.use_alter, info=info,
    ))


def _scope_foreign_keys(table: sa.Table) -> None:
    for constraint in list(table.foreign_key_constraints):
        parent = _parent(constraint)
        referenced = table.metadata.tables.get(parent)
        if referenced is not None and GLOBAL_INFO in referenced.info:
            continue  # a global parent has no tenant to match
        local, remote = constraint.column_keys, [e.target_fullname for e in constraint.elements]
        # A key that already names tenant_id on both sides keeps one pair of it, at the front.
        pairs = [(key, target) for key, target in zip(local, remote) if key != TENANT_COLUMN]
        # Remembered, so a global_table() declared after this one can put the key back.
        info = {**constraint.info, UNSCOPED_INFO: (list(local), remote)}
        _replace_foreign_key(table, constraint, [TENANT_COLUMN, *(key for key, _ in pairs)],
                             [f"{parent}.{TENANT_COLUMN}", *(target for _, target in pairs)], info)


def _unscope_foreign_keys_to(global_: sa.Table) -> None:
    """Foreign keys declared before `global_` was known to be global: back to the columns they named."""
    for table in list(global_.metadata.tables.values()):
        for constraint in list(table.foreign_key_constraints):
            if UNSCOPED_INFO in constraint.info and _parent(constraint) == global_.key:
                local, remote = constraint.info[UNSCOPED_INFO]
                info = {k: v for k, v in constraint.info.items() if k != UNSCOPED_INFO}
                _replace_foreign_key(table, constraint, local, remote, info)


def tenant_table(name: str, metadata: sa.MetaData, *args: Any, **kwargs: Any) -> sa.Table:
    """`sa.Table` with a `tenant_id` leading its primary key, unique constraints and foreign keys."""
    table = sa.Table(name, metadata, sa.Column(TENANT_COLUMN, TenantId(), nullable=False), *args, **kwargs)
    _scope_primary_key(table)
    _scope_uniques(table)
    _scope_foreign_keys(table)
    return table


def global_table(name: str, metadata: sa.MetaData, *args: Any, reason: str, **kwargs: Any) -> sa.Table:
    """A table that holds no tenant's data. Each one is listed, with its reason, in global_tables.toml."""
    if not reason.strip():
        raise MissingReason(name)
    table = sa.Table(name, metadata, *args, **kwargs)
    table.info[GLOBAL_INFO] = reason
    _unscope_foreign_keys_to(table)
    return table


@dataclass(frozen=True)
class Violation:
    table: str
    problem: str

    def __str__(self) -> str:
        return f"{self.table}: {self.problem}"


def registered_global_tables(service: str, registry: Path = REGISTRY) -> set[str]:
    entries = tomllib.loads(registry.read_text()).get("tables", [])
    return {entry["table"] for entry in entries if entry["service"] == service}


def _names(columns: Any) -> str:
    return ", ".join(c.name for c in columns)


def _table_violations(table: sa.Table) -> list[str]:
    if TENANT_COLUMN not in table.c:
        return [f"no {TENANT_COLUMN} column"]
    problems = []
    column = _tenant_column(table)
    if not isinstance(column.type, TenantId):
        problems.append(f"{TENANT_COLUMN} is not a TenantId")
    if column.nullable:
        problems.append(f"{TENANT_COLUMN} is nullable")
    if not table.primary_key.columns or table.primary_key.columns[0] is not column:
        problems.append(f"{TENANT_COLUMN} does not lead the primary key")
    uniques = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)] + [i for i in table.indexes if i.unique]
    for unique in uniques:
        first = unique.expressions[0] if isinstance(unique, sa.Index) else unique.columns[0]
        if first is not column:
            problems.append(f"{TENANT_COLUMN} does not lead unique ({_names(unique.columns)})")
    for foreign_key in table.foreign_key_constraints:
        if GLOBAL_INFO in foreign_key.referred_table.info:
            continue
        target = foreign_key.elements[0].column
        if foreign_key.columns[0] is not column or target.name != TENANT_COLUMN:
            problems.append(f"{TENANT_COLUMN} does not lead foreign key ({_names(foreign_key.columns)})"
                            f" -> {foreign_key.referred_table.name}")
    return problems


def assert_tenant_scoped(metadata: sa.MetaData, service: str, registry: Path = REGISTRY) -> list[Violation]:
    """Every way `service`'s tables fall short of being tenant-scoped; empty when none do."""
    registered = registered_global_tables(service, registry)
    violations = []
    for table in sorted(metadata.tables.values(), key=lambda table: table.name):
        if GLOBAL_INFO in table.info:
            if table.name not in registered:
                violations.append(Violation(table.name, f"global table not registered in {registry.name}"))
            continue
        violations += [Violation(table.name, problem) for problem in _table_violations(table)]
    declared = {table.name for table in metadata.tables.values() if GLOBAL_INFO in table.info}
    for name in sorted(registered - declared):
        violations.append(Violation(name, f"registered in {registry.name} but not declared with global_table()"))
    return violations


class TenantDatabase(Database):
    """A service's database as one tenant sees it: every connection carries the tenant."""

    def __init__(self, database: Database, tenant_id: uuid.UUID) -> None:
        super().__init__(database.service)
        self.database = database
        self.tenant = tenant(tenant_id)
        self.schema = database.schema

    @property  # pyright: ignore[reportIncompatibleVariableOverride]
    def backend(self) -> Backend:
        return self.database.backend

    def url(self, *, asynchronous: bool = True) -> URL:
        return self.database.url(asynchronous=asynchronous)

    @property
    def engine(self) -> AsyncEngine:
        return self.database.engine

    def migration_engine(self) -> Engine:
        return self.database.migration_engine()

    @asynccontextmanager
    async def _scoped(self, connections: Any) -> AsyncIterator[AsyncConnection]:
        async with connections as connection:
            # info lives with the pooled DBAPI connection, so it is cleared before the pool takes it back.
            connection.info[TENANT_COLUMN] = self.tenant
            try:
                yield connection
            finally:
                connection.info.pop(TENANT_COLUMN, None)

    def connect(self) -> Any:
        return self._scoped(self.database.connect())

    def transaction(self) -> Any:
        return self._scoped(self.database.transaction())

    async def close(self) -> None:
        await self.database.close()
