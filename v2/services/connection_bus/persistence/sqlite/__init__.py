"""The connection bus's store on SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from openengine.persistence import DatabaseStore
from openengine.persistence.tenancy import TenantDatabase, new_id

from connection_bus.persistence.schema import connection_routes, connections
from connection_bus.persistence.store import Connection


def _connection(row: Any) -> Connection:
    return Connection(**{key: value for key, value in row._mapping.items() if key != "tenant_id"})


class SqliteConnections(DatabaseStore):
    def __init__(self, database: TenantDatabase) -> None:
        if not isinstance(database, TenantDatabase):
            raise TypeError("connections require a tenant-scoped database")
        super().__init__(database)
        self.tenant = database.tenant

    async def create(self, provider: str, external_id: str, name: str) -> Connection:
        connection = Connection(str(new_id()), provider, external_id, name, datetime.now(UTC))
        async with self.database.transaction() as transaction:
            await transaction.execute(connection_routes.insert().values(
                provider=provider, external_id=external_id, tenant_id=self.tenant,
            ))
            await transaction.execute(connections.insert().values(tenant_id=self.tenant, **vars(connection)))
        return connection

    async def get(self, id: str) -> Connection | None:
        return await self._one(connections.c.id == id)

    async def find(self, provider: str, external_id: str) -> Connection | None:
        return await self._one(sa.and_(connections.c.provider == provider, connections.c.external_id == external_id))

    async def list(self) -> list[Connection]:
        async with self.database.connect() as connection:
            rows = await connection.execute(connections.select().where(
                connections.c.tenant_id == self.tenant,
            ).order_by(connections.c.created_at, connections.c.id))
        return [_connection(row) for row in rows]

    async def _one(self, condition: sa.ColumnElement[bool]) -> Connection | None:
        async with self.database.connect() as connection:
            row = (await connection.execute(connections.select().where(
                connections.c.tenant_id == self.tenant, condition,
            ))).one_or_none()
        return _connection(row) if row else None

    async def rename(self, id: str, name: str) -> None:
        async with self.database.transaction() as transaction:
            await transaction.execute(connections.update().where(
                connections.c.tenant_id == self.tenant, connections.c.id == id,
            ).values(name=name))

    async def delete(self, id: str) -> None:
        async with self.database.transaction() as transaction:
            row = (await transaction.execute(connections.select().where(
                connections.c.tenant_id == self.tenant, connections.c.id == id,
            ))).one_or_none()
            if row is None:
                return
            await transaction.execute(connections.delete().where(
                connections.c.tenant_id == self.tenant, connections.c.id == id,
            ))
            await transaction.execute(connection_routes.delete().where(
                connection_routes.c.tenant_id == self.tenant,
                connection_routes.c.provider == row.provider,
                connection_routes.c.external_id == row.external_id,
            ))
