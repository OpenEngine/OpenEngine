"""The virtual control plane's tables. Migrations must keep the database in step with these."""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime

metadata = sa.MetaData()

workspaces = sa.Table(
    "workspaces",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    # Opaque, provider-owned placement/ownership metadata (a worktree path, a smolvm
    # id, ...). The application layer persists and returns it; only the provider that
    # produced it interprets it, so a restart recovers the same workspace with the
    # same provider instead of silently reprovisioning it elsewhere.
    sa.Column("locator", sa.JSON(), nullable=True),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.Index("ix_workspaces_status", "status"),
    comment="A place an agent does its work, on whichever provider supplied it.",
)

# Managed lifecycle snapshots never adopt rows from the prototype workspaces
# table. Workspace version/state, operation state, and runtime identity/generation
# live in independent documents; only their transaction boundary is shared.
DOCUMENT_COMMENTS = {
    "workspace_records": "The durable workspace lifecycle projection: state, runtime binding and version.",
    "operations": "The durable record of each accepted command's progress, returned by operations.{operation_id}.get.",
    "runtime_ownership": "Which runtime currently owns a workspace, and at which generation.",
    "cursors": "Durable, opaque keyset positions bound to the filters a paginated query was issued with.",
    "outbox": "Public events accepted atomically with their causing state change, not yet published.",
    "command_receipts": "Public command idempotency: a retry with the same command ID returns the original receipt.",
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
