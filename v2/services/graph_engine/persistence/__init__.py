"""Everything the graph engine keeps in its database."""

from __future__ import annotations

from pathlib import Path

from openengine.persistence import Backend, ServicePersistence

from graph_engine.persistence.postgres import PostgresGraphs
from graph_engine.persistence.sqlite import SqliteGraphs
from graph_engine.persistence.store import Graphs

PERSISTENCE = ServicePersistence[Graphs](
    "ge",
    Path(__file__).parent,
    {Backend.SQLITE: SqliteGraphs, Backend.POSTGRES: PostgresGraphs},
)
