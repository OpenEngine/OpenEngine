"""The connection bus's store on SQLite."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from openengine.persistence import DatabaseStore

from connection_bus.persistence.schema import connections
from connection_bus.persistence.store import Connection


def _connection(row: Any) -> Connection:
    return Connection(**row._mapping)


class SqliteConnections(DatabaseStore):
    async def create(self, provider: str, external_id: str, name: str) -> Connection:
        connection = Connection(str(uuid.uuid4()), provider, external_id, name, datetime.now(UTC))
        async with self.database.transaction() as transaction:
            await transaction.execute(connections.insert().values(**vars(connection)))
        return connection

    async def get(self, id: str) -> Connection | None:
        return await self._one(connections.c.id == id)

    async def find(self, provider: str, external_id: str) -> Connection | None:
        return await self._one(sa.and_(connections.c.provider == provider, connections.c.external_id == external_id))

    async def list(self) -> list[Connection]:
        async with self.database.connect() as connection:
            rows = await connection.execute(connections.select().order_by(connections.c.created_at, connections.c.id))
        return [_connection(row) for row in rows]

    async def _one(self, condition: sa.ColumnElement[bool]) -> Connection | None:
        async with self.database.connect() as connection:
            row = (await connection.execute(connections.select().where(condition))).one_or_none()
        return _connection(row) if row else None
