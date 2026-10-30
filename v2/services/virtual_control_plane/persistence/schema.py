"""The VCP lifecycle documents. Alembic owns all schema changes."""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

# Workspace state, operation progress and runtime ownership remain independent.
# Prototype rows share workspaces but have only a legacy body, never a runtime.
DOCUMENT_COMMENTS = {
    "workspaces": "Workspace lifecycle projections, or preserved legacy prototype records.",
    "operations": "Accepted commands' progress and terminal state.",
    "workspace_runtimes": "Provider ownership, opaque locator, runtime identity and generation.",
    "command_receipts": "Immutable command identity and original response for idempotent replay.",
}
documents = {
    name: sa.Table(
        name,
        metadata,
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("body", sa.JSON(), nullable=False),
        comment=comment,
    )
    for name, comment in DOCUMENT_COMMENTS.items()
}

sa.Index(
    "ix_workspaces_legacy_status",
    sa.func.json_extract(documents["workspaces"].c.body, sa.literal_column("'$.legacy.status'")),
    sqlite_where=sa.text("json_type(body, '$.legacy') = 'object'"),
)

# Public events committed with the lifecycle records that caused them. `sequence`
# orders publication; event IDs are opaque and do not. `published_at` stays NULL
# until JetStream acknowledges the event.
outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("sequence", sa.Integer(), primary_key=True),
    sa.Column("workspace_id", sa.String(128), nullable=False),
    sa.Column("event_id", sa.String(128), nullable=False),
    sa.Column("envelope", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    sa.Index("ix_outbox_event_id", "event_id", unique=True),
    sa.Index("ix_outbox_pending", "sequence", sqlite_where=sa.text("published_at IS NULL")),
    sa.Index("ix_outbox_workspace_pending", "workspace_id", "sequence", sqlite_where=sa.text("published_at IS NULL")),
    comment="Immutable public event envelopes awaiting or recording JetStream publication.",
    sqlite_autoincrement=True,
)
