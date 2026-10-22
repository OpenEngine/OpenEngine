"""SQLite unit of work. BEGIN IMMEDIATE serializes acceptance across connections."""
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert

from openengine.persistence import Backend
from openengine.persistence.database import BackendNotImplemented
from virtual_control_plane.persistence.schema import documents


class Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def get(self, collection, key):
        table = documents[collection]
        return (await self.connection.execute(sa.select(table.c.body).where(table.c.key == key))).scalar_one_or_none()

    async def put(self, collection, key, value):
        table = documents[collection]
        statement = insert(table).values(key=key, body=value)
        await self.connection.execute(statement.on_conflict_do_update(index_elements=['key'], set_={'body': value}))

    async def all(self, collection):
        table = documents[collection]
        return list((await self.connection.execute(sa.select(table.c.body).order_by(table.c.key))).scalars())


class DurableStore:
    def __init__(self, database):
        if database.backend != Backend.SQLITE:
            raise BackendNotImplemented(database.backend, 'durable VCP operations')
        self.database = database

    @asynccontextmanager
    async def transaction(self):
        async with self.database.connect() as connection:
            await connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                yield Transaction(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def close(self):
        await self.database.close()
