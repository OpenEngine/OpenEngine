"""SQLite lifecycle transactions. Schema changes belong exclusively in Alembic."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from openengine.persistence import Backend, BackendNotImplemented, Database

from virtual_control_plane.application.transactions import Collection, CommandIdConflict, Document
from virtual_control_plane.persistence.schema import documents


def _identity(subject: str, command: Document) -> str:
    # Snapshot only immutable public identity. A retry's deadline is irrelevant.
    return json.dumps(
        [subject, command["payload"], command.get("expected_version"), command["causal_path"]],
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

    async def close(self) -> None:
        await self.database.close()
