"""What a service's persistence/ directory declares to the shared layer.

    persistence/
      __init__.py    PERSISTENCE = ServicePersistence(...)
      store.py       the Protocol the rest of the service depends on
      sqlite/        the store, written for SQLite
      postgres/      the store, written for Postgres
      migrations/    one Alembic history for both

The rest of the service asks for `PERSISTENCE.open(config).scoped(tenant)` and
gets whichever store the config selects, typed as the service's own Protocol
and bound to that tenant. There is no way to a store without one.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from sqlalchemy import MetaData

from openengine.persistence import migrations
from openengine.persistence.config import Backend, DatabaseConfig
from openengine.persistence.database import BackendNotImplemented, Database, open_database
from openengine.persistence.tenancy import TenantDatabase, tenant

StoreT = TypeVar("StoreT")


class OpenPersistence(Generic[StoreT]):
    """A service's open database, whose stores are only handed out for one tenant at a time."""

    __slots__ = ("_database", "_factory")

    def __init__(self, database: Database, factory: Callable[[Database], StoreT]) -> None:
        self._database = database
        self._factory = factory

    def scoped(self, tenant_id: uuid.UUID) -> StoreT:
        """The store over a database whose every connection and transaction carries `tenant_id`."""
        return self._factory(TenantDatabase(self._database, tenant(tenant_id)))

    async def close(self) -> None:
        await self._database.close()


@dataclass(frozen=True)
class ServicePersistence(Generic[StoreT]):
    name: str
    directory: Path
    stores: Mapping[Backend, Callable[[Database], StoreT]]

    @property
    def migrations(self) -> Path:
        return self.directory / "migrations"

    def database(self, config: DatabaseConfig | None = None) -> Database:
        return open_database(self.name, config)

    def open(self, config: DatabaseConfig | None = None) -> OpenPersistence[StoreT]:
        database = self.database(config)
        try:
            factory = self.stores[database.backend]
        except KeyError:
            raise BackendNotImplemented(database.backend, f"{self.name}'s store") from None
        return OpenPersistence(database, factory)

    def upgrade(self, config: DatabaseConfig | None = None, revision: str = "head") -> migrations.Upgrade:
        return migrations.upgrade(self.migrations, self.database(config), revision)

    def drift(self, metadata: MetaData, config: DatabaseConfig | None = None) -> list[Any]:
        """How the migrated database differs from `metadata`; empty when the migrations match the schema."""
        return migrations.drift(self.database(config), metadata)
