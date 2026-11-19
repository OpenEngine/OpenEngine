"""What every service's store has in common, whichever backend it is written for."""

from __future__ import annotations

from typing import Protocol

from openengine.persistence.tenancy import TenantDatabase


class Store(Protocol):
    async def close(self) -> None: ...


class DatabaseStore:
    """A store that owns its database and releases it on close."""

    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    async def close(self) -> None:
        await self.database.close()
