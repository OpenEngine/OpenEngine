"""The connection bus's tables. Migrations must keep the database in step with these."""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime

metadata = sa.MetaData()

# One per external account OpenEngine hears from: a GitHub App installation, a Slack workspace.
connections = sa.Table(
    "connections",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("external_id", sa.String(255), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("provider", "external_id", name="uq_connections_provider_external_id"),
)
