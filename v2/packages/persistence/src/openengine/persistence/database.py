"""A service's database, whichever backend holds it.

Services talk to `Database` and never to a backend's module, so the config
alone decides where their data lives.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import ClassVar

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from openengine.persistence.config import Backend, DatabaseConfig


class BackendNotImplemented(NotImplementedError):
    def __init__(self, backend: Backend, what: str) -> None:
        super().__init__(f"the {backend} backend does not implement {what} yet")


class Database(ABC):
    backend: ClassVar[Backend]
    # The schema that holds the service's tables and Alembic's version table on
    # backends that share one database between services. None keeps the default.
    schema: str | None = None

    def __init__(self, service: str) -> None:
        self.service = service
        self._engine: AsyncEngine | None = None
        self.write_lock = asyncio.Lock()

    @abstractmethod
    def url(self, *, asynchronous: bool = True) -> URL:
        """Where the service's data lives, with the async or the sync driver."""

    def prepare(self) -> None:
        """Make anything the database needs before the first connection."""

    def configure(self, engine: Engine) -> None:
        """Set up connections as the pool opens them."""

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self.prepare()
            self._engine = create_async_engine(self.url())
            self.configure(self._engine.sync_engine)
        return self._engine

    def migration_engine(self) -> Engine:
        """A synchronous engine for Alembic, left unconfigured so migrations see the database as it is."""
        self.prepare()
        return create_engine(self.url(asynchronous=False), poolclass=NullPool)

    def connect(self) -> AbstractAsyncContextManager[AsyncConnection]:
        return self.engine.connect()

    async def transaction_started(self, connection: AsyncConnection, tenant: uuid.UUID) -> None:
        """Hook after BEGIN, before store SQL. Use transaction-local state only; never end the transaction."""

    @asynccontextmanager
    async def transaction(self, tenant: uuid.UUID, *, immediate: bool = False) -> AsyncIterator[AsyncConnection]:
        """Bind a credential-derived tenant, commit on success and roll back on every other exit.

        `immediate` reserves SQLite's writer before a read/modify/write operation.
        """
        from openengine.persistence.tenancy import tenant as validate_tenant

        tenant = validate_tenant(tenant)
        if immediate and self.backend != Backend.SQLITE:
            raise BackendNotImplemented(self.backend, "immediate transactions")
        connection = await self.engine.connect()
        # Keep the original dictionary: accessing connection.info after invalidation
        # can reconnect or mask the original error. Clear it even when discarded.
        info = connection.info
        try:
            await connection.begin()
            if self.backend == Backend.SQLITE:
                await connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                # Psycopg sends BEGIN before its first statement. An explicit
                # BEGIN here would start it twice; the hook must see INTRANS.
                await connection.exec_driver_sql("SELECT 1")
            info["tenant_id"] = tenant
            await self.transaction_started(connection, tenant)
            yield connection
            await connection.commit()
        finally:
            info.pop("tenant_id", None)
            await _close_connection(connection)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None


def open_database(service: str, config: DatabaseConfig | None = None) -> Database:
    from openengine.persistence.postgres import PostgresDatabase
    from openengine.persistence.sqlite import SqliteDatabase

    config = config or DatabaseConfig.from_environment()
    match config.backend:
        case Backend.SQLITE:
            return SqliteDatabase(service, config.sqlite_directory)
        case Backend.POSTGRES:
            return PostgresDatabase(service, config.postgres_url, config.postgres_migration_url)


async def _close_connection(connection: AsyncConnection) -> None:
    """Finish rollback/checkin before returning, even if cancellation repeats during cleanup."""
    async def close() -> None:
        try:
            await connection.close()
        except BaseException:
            await connection.invalidate()
            await connection.close()
            raise

    task = asyncio.create_task(close())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError
