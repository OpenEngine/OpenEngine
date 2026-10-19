"""The graph engine's tables. Migrations must keep the database in step with these."""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime

metadata = sa.MetaData()

# Every registered version of every graph; registering a graph again adds the next version.
graphs = sa.Table(
    "graphs",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("definition", sa.JSON, nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("name", "version", name="uq_graphs_name_version"),
)
