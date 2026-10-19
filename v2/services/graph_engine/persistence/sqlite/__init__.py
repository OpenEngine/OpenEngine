"""The graph engine's store on SQLite."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from openengine.persistence import DatabaseStore

from graph_engine.persistence.schema import graphs
from graph_engine.persistence.store import Graph


def _graph(row: Any) -> Graph:
    return Graph(**row._mapping)


class SqliteGraphs(DatabaseStore):
    async def register(self, name: str, definition: dict[str, Any]) -> Graph:
        async with self.database.transaction() as transaction:
            current = await transaction.scalar(sa.select(sa.func.max(graphs.c.version)).where(graphs.c.name == name))
            graph = Graph(str(uuid.uuid4()), name, (current or 0) + 1, definition, datetime.now(UTC))
            await transaction.execute(graphs.insert().values(**vars(graph)))
        return graph

    async def get(self, id: str) -> Graph | None:
        async with self.database.connect() as connection:
            row = (await connection.execute(graphs.select().where(graphs.c.id == id))).one_or_none()
        return _graph(row) if row else None

    async def latest(self, name: str) -> Graph | None:
        query = graphs.select().where(graphs.c.name == name).order_by(graphs.c.version.desc()).limit(1)
        async with self.database.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return _graph(row) if row else None

    async def versions(self, name: str) -> list[Graph]:
        async with self.database.connect() as connection:
            rows = await connection.execute(graphs.select().where(graphs.c.name == name).order_by(graphs.c.version))
        return [_graph(row) for row in rows]
