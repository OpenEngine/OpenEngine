"""Everything the virtual control plane keeps in its database."""

from __future__ import annotations

from pathlib import Path

from openengine.persistence import Backend, ServicePersistence

from virtual_control_plane.persistence.postgres import PostgresWorkspaces
from virtual_control_plane.persistence.sqlite import SqliteWorkspaces
from virtual_control_plane.persistence.store import Workspaces

PERSISTENCE = ServicePersistence[Workspaces](
    "vcp",
    Path(__file__).parent,
    {Backend.SQLITE: SqliteWorkspaces, Backend.POSTGRES: PostgresWorkspaces},
)
