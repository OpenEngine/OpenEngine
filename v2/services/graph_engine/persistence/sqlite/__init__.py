"""The graph engine's store on SQLite.

GE is the database's only writer, and its commands read before they write, so
a lock makes them take turns: concurrent applies of one graph can neither
number two revisions alike nor both record one command ID. The tables' keys
refuse either anyway.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from openengine.persistence import Database, DatabaseStore

from graph_engine.persistence.schema import command_receipts, graph_revisions, graphs, operations
from graph_engine.persistence.store import (
    Command,
    CommandIdConflict,
    DeadlineExceeded,
    Definition,
    Graph,
    GraphRevision,
    Operation,
    VersionConflict,
)

APPLY = "graphs.apply"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class SqliteGraphs(DatabaseStore):
    def __init__(self, database: Database) -> None:
        super().__init__(database)
        self._writing = asyncio.Lock()

    async def apply(
        self, command: Command, definition: Definition, expected_version: int | None = None,
        deadline: datetime | None = None,
    ) -> dict[str, Any]:
        async with self._writing, self.database.transaction() as transaction:
            if (receipt := await _receipt(transaction, command)) is not None:
                return receipt
            now = datetime.now(UTC)
            # Checked once the lock is held, so waiting for it cannot carry an acceptance past the deadline.
            if deadline is not None and deadline < now:
                raise DeadlineExceeded(deadline, now)
            graph = (await transaction.execute(graphs.select().where(graphs.c.name == definition.name))).one_or_none()
            if expected_version is not None and (graph is None or graph.latest_revision != expected_version):
                raise VersionConflict(expected_version, graph.latest_revision if graph else None)
            latest = graph and await transaction.scalar(
                sa.select(graph_revisions.c.source_sha256).where(
                    graph_revisions.c.graph_id == graph.graph_id, graph_revisions.c.revision == graph.latest_revision
                )
            )
            created = latest != definition.source_sha256
            if graph is None:
                graph_id, revision = _new_id("graph"), 1
                await transaction.execute(graphs.insert().values(
                    graph_id=graph_id, name=definition.name, description=definition.description,
                    latest_revision=revision, created_at=now, updated_at=now,
                ))
            elif created:
                graph_id, revision = graph.graph_id, graph.latest_revision + 1
                await transaction.execute(graphs.update().where(graphs.c.graph_id == graph_id).values(
                    description=definition.description, latest_revision=revision, updated_at=now,
                ))
            else:
                graph_id, revision = graph.graph_id, graph.latest_revision
            if created:
                await transaction.execute(graph_revisions.insert().values(
                    graph_id=graph_id, revision=revision, format=definition.format, source=definition.source,
                    source_sha256=definition.source_sha256, created_at=now,
                ))
            operation_id = _new_id("op")
            # Applying is done once it is recorded, so the operation is recorded as succeeded.
            await transaction.execute(operations.insert().values(
                operation_id=operation_id, command_id=command.command_id, operation=APPLY, status="succeeded",
                resources={"graph_id": graph_id},
                result={"graph": {"graph_id": graph_id, "revision": revision}, "created": created},
                created_at=now, updated_at=now,
            ))
            response = {
                "command_id": command.command_id, "status": "accepted", "operation_id": operation_id, "resource_id": graph_id,
            }
            await _record(transaction, command, response, now)
        return response

    async def reject(self, command: Command, response: dict[str, Any]) -> dict[str, Any]:
        async with self._writing, self.database.transaction() as transaction:
            if (receipt := await _receipt(transaction, command)) is not None:
                return receipt
            await _record(transaction, command, response, datetime.now(UTC))
        return response

    async def graph(self, graph_id: str) -> Graph | None:
        async with self.database.connect() as connection:
            row = (await connection.execute(graphs.select().where(graphs.c.graph_id == graph_id))).one_or_none()
        return Graph(**row._mapping) if row else None

    async def revision(self, graph_id: str, revision: int) -> GraphRevision | None:
        query = graph_revisions.select().where(
            graph_revisions.c.graph_id == graph_id, graph_revisions.c.revision == revision
        )
        async with self.database.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return GraphRevision(**row._mapping) if row else None

    async def operation(self, operation_id: str) -> Operation | None:
        query = operations.select().where(operations.c.operation_id == operation_id)
        async with self.database.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return Operation(**row._mapping) if row else None


async def _receipt(transaction: AsyncConnection, command: Command) -> dict[str, Any] | None:
    """The response recorded for `command`'s ID, if it has one; CommandIdConflict if it was another command."""
    query = command_receipts.select().where(command_receipts.c.command_id == command.command_id)
    receipt = (await transaction.execute(query)).one_or_none()
    if receipt is None:
        return None
    if receipt.fingerprint != command.fingerprint:
        raise CommandIdConflict(command.command_id, receipt.subject, receipt.response.get("operation_id"))
    return receipt.response


async def _record(transaction: AsyncConnection, command: Command, response: dict[str, Any], now: datetime) -> None:
    await transaction.execute(command_receipts.insert().values(
        command_id=command.command_id, subject=command.subject, fingerprint=command.fingerprint,
        response=response, created_at=now,
    ))
