"""SQLite lifecycle transactions. Schema changes belong exclusively in Alembic."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from openengine.persistence import Backend, BackendNotImplemented, Database

from virtual_control_plane.application.transactions import (
    Collection, CommandIdConflict, Document, InvalidEvent, PendingEvent,
)
from virtual_control_plane.jetstream import event_errors
from virtual_control_plane.persistence.schema import documents, outbox


# Enumerate workspace IDs by index seeks, rather than DISTINCT/GROUP BY scans.
# The second recursive CTE is a priority queue: SQLite's recursive ORDER BY
# expands the smallest sequence first and LIMIT bounds the whole result. Each
# expansion seeks the next event of just that workspace, so excluded backlogs
# are never walked. Work is proportional to pending workspaces plus batch size.
_PENDING_EXCLUDING = sa.text("""
WITH RECURSIVE workspaces(workspace_id) AS (
    SELECT min(workspace_id) FROM outbox WHERE published_at IS NULL
    UNION ALL
    SELECT (SELECT min(workspace_id) FROM outbox
            WHERE published_at IS NULL AND workspace_id > workspaces.workspace_id)
    FROM workspaces WHERE workspace_id IS NOT NULL
),
pending(sequence, workspace_id) AS (
    SELECT event.sequence, event.workspace_id
    FROM workspaces JOIN outbox AS event ON event.sequence = (
        SELECT min(sequence) FROM outbox
        WHERE published_at IS NULL AND workspace_id = workspaces.workspace_id
    )
    WHERE workspaces.workspace_id NOT IN :exclude
    UNION ALL
    SELECT event.sequence, event.workspace_id
    FROM pending JOIN outbox AS event ON event.sequence = (
        SELECT min(sequence) FROM outbox
        WHERE published_at IS NULL AND workspace_id = pending.workspace_id
          AND sequence > pending.sequence
    )
    ORDER BY 1 LIMIT :limit
)
SELECT event.sequence, event.workspace_id, event.event_id, event.envelope
FROM pending JOIN outbox AS event ON event.sequence = pending.sequence
ORDER BY event.sequence
""").bindparams(sa.bindparam("exclude", expanding=True)).columns(
    sequence=sa.Integer, workspace_id=sa.String, event_id=sa.String, envelope=sa.JSON,
)


def _normalize_numbers(value: object) -> object:
    # JSON Schema integers include 1.0. Canonicalize equivalent JSON numbers,
    # including those nested in payloads, without conflating booleans or strings.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    return value


def _identity(subject: str, command: Document) -> str:
    # Snapshot only immutable public identity. A retry's deadline is irrelevant.
    return json.dumps(
        _normalize_numbers([subject, command["payload"], command.get("expected_version"), command["causal_path"]]),
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _collection(collection: Collection) -> sa.Table:
    # Receipts cannot be overwritten through the document API, even by untyped callers.
    if collection == "command_receipts":
        raise KeyError(collection)
    return documents[collection]


class Transaction:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get(self, collection: Collection, key: str) -> Document | None:
        table = _collection(collection)
        return (await self._connection.execute(sa.select(table.c.body).where(table.c.key == key))).scalar_one_or_none()

    async def put(self, collection: Collection, key: str, value: Document) -> None:
        table = _collection(collection)
        statement = insert(table).values(key=key, body=value)
        await self._connection.execute(statement.on_conflict_do_update(index_elements=["key"], set_={"body": value}))

    async def all(self, collection: Collection) -> list[Document]:
        table = _collection(collection)
        return list((await self._connection.execute(sa.select(table.c.body).order_by(table.c.key))).scalars())

    async def replay(self, subject: str, command: Document) -> Document | None:
        table = documents["command_receipts"]
        receipt = (await self._connection.execute(
            sa.select(table.c.body).where(table.c.key == command["command_id"])
        )).scalar_one_or_none()
        if receipt is None:
            return None
        if receipt["identity"] != _identity(subject, command):
            raise CommandIdConflict(command["command_id"])
        return receipt["response"]

    async def record_command(self, subject: str, command: Document, response: Document) -> Document:
        original = await self.replay(subject, command)
        if original is not None:
            return original
        if response.get("error", {}).get("retryable", False):
            return response
        await self._connection.execute(documents["command_receipts"].insert().values(
            key=command["command_id"], body={"identity": _identity(subject, command), "response": response},
        ))
        return response

    async def append_event(self, envelope: Document) -> None:
        # Reject what could never be published before it can hold back its workspace.
        if errors := event_errors(envelope):
            raise InvalidEvent(errors)
        await self._connection.execute(outbox.insert().values(
            workspace_id=envelope["resources"]["workspace_id"], event_id=envelope["event_id"],
            envelope=envelope, created_at=datetime.now(UTC),
        ))


class DurableStore:
    def __init__(self, database: Database) -> None:
        if database.backend != Backend.SQLITE:
            raise BackendNotImplemented(database.backend, "durable VCP operations")
        self.database = database

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self.database.connect() as connection:
            try:
                # Acquire the writer reservation *before* reading a receipt. A
                # deferred transaction could race between lookup and insertion.
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield Transaction(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def pending_events(self, limit: int, exclude: Iterable[str] = ()) -> list[PendingEvent]:
        if excluded := list(exclude):
            query = _PENDING_EXCLUDING.bindparams(exclude=excluded, limit=limit)
        else:
            # With no blocked workspace the sequence-only index already gives a bounded scan.
            query = (
                sa.select(outbox.c.sequence, outbox.c.workspace_id, outbox.c.event_id, outbox.c.envelope)
                .where(outbox.c.published_at.is_(None))
                .order_by(outbox.c.sequence).limit(limit)
            )
        async with self.database.connect() as connection:
            return [PendingEvent(**row) for row in (await connection.execute(query)).mappings()]

    async def mark_published(self, sequence: int) -> None:
        async with self.database.connect() as connection:
            await connection.execute(
                outbox.update().where(outbox.c.sequence == sequence, outbox.c.published_at.is_(None))
                .values(published_at=datetime.now(UTC))
            )
            await connection.commit()

    async def close(self) -> None:
        await self.database.close()
