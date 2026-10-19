"""The virtual control plane's store on SQLite."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from openengine.persistence import DatabaseStore

from virtual_control_plane.persistence.schema import workspaces
from virtual_control_plane.persistence.store import Workspace, WorkspaceProvider, WorkspaceStatus


def _workspace(row: Any) -> Workspace:
    values = row._mapping
    return Workspace(
        id=values["id"],
        provider=WorkspaceProvider(values["provider"]),
        status=WorkspaceStatus(values["status"]),
        created_at=values["created_at"],
        updated_at=values["updated_at"],
    )


class SqliteWorkspaces(DatabaseStore):
    async def create(self, provider: WorkspaceProvider) -> Workspace:
        now = datetime.now(UTC)
        workspace = Workspace(str(uuid.uuid4()), provider, WorkspaceStatus.PROVISIONING, now, now)
        async with self.database.transaction() as transaction:
            await transaction.execute(workspaces.insert().values(**vars(workspace)))
        return workspace

    async def get(self, id: str) -> Workspace | None:
        async with self.database.connect() as connection:
            row = (await connection.execute(workspaces.select().where(workspaces.c.id == id))).one_or_none()
        return _workspace(row) if row else None

    async def set_status(self, id: str, status: WorkspaceStatus) -> Workspace | None:
        update = (
            workspaces.update()
            .where(workspaces.c.id == id)
            .values(status=status, updated_at=datetime.now(UTC))
            .returning(*workspaces.c)
        )
        async with self.database.transaction() as transaction:
            row = (await transaction.execute(update)).one_or_none()
        return _workspace(row) if row else None

    async def list(self, status: WorkspaceStatus | None = None) -> list[Workspace]:
        query = workspaces.select().order_by(workspaces.c.created_at, workspaces.c.id)
        if status is not None:
            query = query.where(workspaces.c.status == status)
        async with self.database.connect() as connection:
            rows = await connection.execute(query)
        return [_workspace(row) for row in rows]
