"""A service's database, whichever backend holds it.

Services talk to `Database` and never to a backend's module, so the config
alone decides where their data lives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
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

    def transaction(self) -> AbstractAsyncContextManager[AsyncConnection]:
        """A connection that commits when the block exits cleanly and rolls back otherwise."""
        return self.engine.begin()

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
            return PostgresDatabase(service, config.postgres_url)
