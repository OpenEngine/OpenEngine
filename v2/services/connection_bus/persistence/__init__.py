"""Everything the connection bus keeps in its database."""

from __future__ import annotations

from pathlib import Path

from openengine.persistence import Backend, ServicePersistence

from connection_bus.persistence.postgres import PostgresConnections
from connection_bus.persistence.sqlite import SqliteConnections
from connection_bus.persistence.store import Connections

PERSISTENCE = ServicePersistence[Connections](
    "cxn",
    Path(__file__).parent,
    {Backend.SQLITE: SqliteConnections, Backend.POSTGRES: PostgresConnections},
)
