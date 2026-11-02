"""The daemon's runtime-local journal: one SQLite file per runtime generation.

Alembic (`migrations/`) owns the schema; opening the store never creates it.
This is not a tenant service database: it lives with the runtime, holds one
workspace's binding, and is never discovered as a VCP-side service store.
Updates keep their exact encoded bytes after acknowledgement, for replay.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from openengine.persistence import migrations
from openengine.persistence.sqlite import SqliteDatabase

MIGRATIONS = Path(__file__).parent / "migrations"
NAME = "workspace-daemon"

Collection = Literal["meta", "commands", "attempts", "sessions"]

metadata = sa.MetaData()
documents = sa.Table(
    "journal_documents", metadata,
    sa.Column("collection", sa.String(32), primary_key=True),
    sa.Column("key", sa.String(128), primary_key=True),
    sa.Column("body", sa.JSON(), nullable=False),
    comment="Runtime binding, command receipts, attempts and session ownership.",
)
updates = sa.Table(
    "journal_updates", metadata,
    sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
    sa.Column("event_id", sa.String(128), nullable=False),
    sa.Column("wire", sa.LargeBinary(), nullable=False),
    sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Index("ix_journal_updates_event_id", "event_id", unique=True),
    sa.Index("ix_journal_updates_pending", "sequence", sqlite_where=sa.text("acknowledged = 0")),
    comment="Ordered runtime updates as sent, retained after VCP commits them.",
)


def database(directory: Path) -> SqliteDatabase:
    return SqliteDatabase(NAME, directory)


def upgrade(journal: SqliteDatabase) -> migrations.Upgrade:
    return migrations.upgrade(MIGRATIONS, journal)


class Transaction:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get(self, collection: Collection, key: str) -> dict[str, Any] | None:
        query = sa.select(documents.c.body).where(documents.c.collection == collection, documents.c.key == key)
        return (await self._connection.execute(query)).scalar_one_or_none()

    async def put(self, collection: Collection, key: str, body: dict[str, Any]) -> None:
        statement = insert(documents).values(collection=collection, key=key, body=body)
        await self._connection.execute(statement.on_conflict_do_update(
            index_elements=["collection", "key"], set_={"body": body}))

    async def all(self, collection: Collection) -> list[dict[str, Any]]:
        query = sa.select(documents.c.body).where(documents.c.collection == collection).order_by(documents.c.key)
        return list((await self._connection.execute(query)).scalars())

    async def append_update(self, sequence: int, event_id: str, wire: bytes) -> None:
        await self._connection.execute(updates.insert().values(sequence=sequence, event_id=event_id, wire=wire))

    async def oldest_pending(self) -> tuple[int, bytes] | None:
        query = (sa.select(updates.c.sequence, updates.c.wire).where(updates.c.acknowledged.is_(False))
                 .order_by(updates.c.sequence).limit(1))
        row = (await self._connection.execute(query)).first()
        return None if row is None else (row.sequence, row.wire)

    async def has_update(self, sequence: int) -> bool:
        query = sa.select(updates.c.sequence).where(updates.c.sequence == sequence)
        return (await self._connection.execute(query)).first() is not None

    async def acknowledge(self, sequence: int) -> None:
        await self._connection.execute(updates.update().where(updates.c.sequence == sequence).values(acknowledged=True))

    async def replay_from(self, sequence: int) -> None:
        """Resend retained updates from `sequence`, unchanged, before anything later."""
        await self._connection.execute(updates.update().where(updates.c.sequence >= sequence).values(acknowledged=False))


class JournalStore:
    def __init__(self, journal: SqliteDatabase) -> None:
        self.database = journal

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self.database.connect() as connection:
            try:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield Transaction(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Transaction]:
        """A read-only view that never reserves the writer, so inspect stays responsive."""
        async with self.database.connect() as connection:
            yield Transaction(connection)

    async def close(self) -> None:
        await self.database.close()
