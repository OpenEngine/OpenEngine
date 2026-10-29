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
