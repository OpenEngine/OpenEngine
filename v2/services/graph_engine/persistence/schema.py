"""The graph engine's tables. Migrations must keep the database in step with these."""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime

metadata = sa.MetaData()

# One row per graph name. Its ID never changes; applying a changed definition adds a revision.
graphs = sa.Table(
    "graphs",
    metadata,
    sa.Column("graph_id", sa.String(128), primary_key=True),
    sa.Column("name", sa.String(64), nullable=False),
    sa.Column("description", sa.String(2000)),
    sa.Column("latest_revision", sa.Integer, nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("name", name="uq_graphs_name"),
)

# Every revision of every graph, numbered from 1 and never changed once written.
graph_revisions = sa.Table(
    "graph_revisions",
    metadata,
    sa.Column("graph_id", sa.String(128), sa.ForeignKey("graphs.graph_id"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("format", sa.String(16), nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("source_sha256", sa.String(64), nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
)

# The durable record of each accepted command's progress, which operations.{operation_id}.get reads.
operations = sa.Table(
    "operations",
    metadata,
    sa.Column("operation_id", sa.String(128), primary_key=True),
    sa.Column("command_id", sa.String(128), nullable=False),
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("resources", sa.JSON, nullable=False),
    sa.Column("result", sa.JSON),
    sa.Column("error", sa.JSON),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("command_id", name="uq_operations_command_id"),
)

# The reply to every command GE accepted or rejected for good, so a retry with its command ID gets the same one.
command_receipts = sa.Table(
    "command_receipts",
    metadata,
    sa.Column("command_id", sa.String(128), primary_key=True),
    sa.Column("subject", sa.String(255), nullable=False),
    sa.Column("fingerprint", sa.String(64), nullable=False),
    sa.Column("response", sa.JSON, nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
)
