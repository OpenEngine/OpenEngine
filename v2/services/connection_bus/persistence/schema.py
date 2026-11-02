"""The connection bus's tables. Migrations must keep the database in step with these."""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime
from openengine.persistence.tenancy import TenantId, global_table, tenant_table

metadata = sa.MetaData()

connections = tenant_table(
    "connections",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("external_id", sa.String(255), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("provider", "external_id", name="uq_connections_provider_external_id"),
    comment="One per external account OpenEngine hears from: a GitHub App installation, a Slack workspace.",
)

connection_routes = global_table(
    "connection_routes",
    metadata,
    sa.Column("provider", sa.String(64), primary_key=True),
    sa.Column("external_id", sa.String(255), primary_key=True),
    sa.Column("tenant_id", TenantId(), nullable=False),
    comment="Global ownership route for authenticated provider webhooks.",
    reason="Maps (provider, external_id) to its tenant before the tenant is known",
)
