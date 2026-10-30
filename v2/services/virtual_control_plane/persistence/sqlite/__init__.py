"""Prototype workspace access on the consolidated document table.

Only legacy bodies belong to this adapter. Managed lifecycle documents are
owned by DurableStore; a prototype row never implies managed runtime ownership.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from openengine.persistence import DatabaseStore

from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.persistence.schema import documents
from virtual_control_plane.persistence.store import Locator, Workspace, WorkspaceProvider, WorkspaceStatus


def _workspace(body: dict[str, Any] | None) -> Workspace | None:
    if body is None or "legacy" not in body:
        return None
    values = body["legacy"]
    return Workspace(
        id=values["id"],
        provider=WorkspaceProvider(values["provider"]),
        status=WorkspaceStatus(values["status"]),
        created_at=datetime.fromisoformat(values["created_at"]),
        updated_at=datetime.fromisoformat(values["updated_at"]),
        locator=values["locator"],
    )


class SqliteWorkspaces(DatabaseStore):
    async def create(self, provider: WorkspaceProvider, locator: Locator | None = None) -> Workspace:
        now = datetime.now(UTC)
        workspace = Workspace(str(uuid.uuid4()), provider, WorkspaceStatus.PROVISIONING, now, now, locator)
        body = {**vars(workspace), "created_at": now.isoformat(), "updated_at": now.isoformat()}
        async with DurableStore(self.database).transaction() as tx:
            await tx.put("workspaces", workspace.id, {"legacy": body})
        return workspace

    async def get(self, id: str) -> Workspace | None:
        table = documents["workspaces"]
        async with self.database.connect() as connection:
            body = (await connection.execute(sa.select(table.c.body).where(table.c.key == id))).scalar_one_or_none()
        return _workspace(body)

    async def _update(self, id: str, **changes: Any) -> Workspace | None:
        async with DurableStore(self.database).transaction() as tx:
            body = await tx.get("workspaces", id)
            if _workspace(body) is None:
                return None
            body["legacy"].update(changes, updated_at=datetime.now(UTC).isoformat())
            await tx.put("workspaces", id, body)
            return _workspace(body)

    async def set_status(self, id: str, status: WorkspaceStatus) -> Workspace | None:
        return await self._update(id, status=status)

    async def set_locator(self, id: str, locator: Locator) -> Workspace | None:
        return await self._update(id, locator=locator)

    async def list(self, status: WorkspaceStatus | None = None) -> list[Workspace]:
        table = documents["workspaces"]
        # Literal JSON paths must match the migration's expression index.
        query = sa.select(table.c.body).where(sa.text("json_type(body, '$.legacy') = 'object'"))
        if status is not None:
            query = query.where(sa.literal_column("json_extract(body, '$.legacy.status')") == status.value)
        async with self.database.connect() as connection:
            rows = (await connection.execute(query)).scalars()
        workspaces = [workspace for body in rows if (workspace := _workspace(body)) is not None]
        return sorted(
            workspaces,
            key=lambda workspace: (workspace.created_at, workspace.id),
        )
