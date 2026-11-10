"""Durable ownership rows for internal runtime broker credentials."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert

from openengine.persistence import TenantDatabase

from virtual_control_plane.persistence.schema import runtime_credentials


@dataclass(frozen=True)
class CredentialRow:
    runtime_id: str
    generation: int
    username: str
    status: str
    issued_at: datetime
    revoked_at: datetime | None
    secret_digest: str | None


class CredentialRows:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    async def get(self, runtime_id: str) -> CredentialRow | None:
        query = sa.select(runtime_credentials).where(runtime_credentials.c.runtime_id == runtime_id)
        async with self.database.transaction() as connection:
            row = (await connection.execute(query)).mappings().one_or_none()
        return CredentialRow(**row) if row else None

    async def with_status(self, *statuses: str) -> list[CredentialRow]:
        query = (sa.select(runtime_credentials).where(runtime_credentials.c.status.in_(statuses))
                 .order_by(runtime_credentials.c.runtime_id))
        async with self.database.transaction() as connection:
            return [CredentialRow(**row) for row in (await connection.execute(query)).mappings()]

    async def claim(self, runtime_id: str, generation: int, username: str) -> CredentialRow:
        """The runtime's row, inserting it as `active` (with no secret yet) if absent."""
        async with self.database.transaction() as connection:
            await connection.execute(insert(runtime_credentials).values(
                runtime_id=runtime_id, generation=generation, username=username, status="active",
                issued_at=datetime.now(UTC)).on_conflict_do_nothing(index_elements=["runtime_id"]))
        return await self.get(runtime_id)

    async def record_digest(self, runtime_id: str, digest: str) -> bool:
        async with self.database.transaction() as connection:
            result = await connection.execute(runtime_credentials.update().where(
                runtime_credentials.c.runtime_id == runtime_id, runtime_credentials.c.status == "active",
                runtime_credentials.c.secret_digest.is_(None)).values(secret_digest=digest))
        return result.rowcount == 1

    async def transition(self, runtime_id: str, before: str, after: str) -> bool:
        values = {"status": after}
        if after == "retired":
            values["revoked_at"] = datetime.now(UTC)
        async with self.database.transaction() as connection:
            result = await connection.execute(runtime_credentials.update().where(
                runtime_credentials.c.runtime_id == runtime_id, runtime_credentials.c.status == before).values(**values))
        return result.rowcount == 1
