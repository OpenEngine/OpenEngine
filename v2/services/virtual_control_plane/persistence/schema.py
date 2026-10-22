"""The virtual control plane's tables. Migrations must keep the database in step with these."""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime

metadata = sa.MetaData()

# A place an agent does its work, on whichever provider supplied it.
workspaces = sa.Table(
    "workspaces",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.Index("ix_workspaces_status", "status"),
)

# Durable documents are deliberately separate from the original prototype
# workspace table. Indexed identities and transaction boundaries are stable;
# document bodies are the application snapshots, not SQLAlchemy domain objects.
documents = {}
for name in ('workspace_records', 'command_receipts', 'operations', 'runtime_ownership', 'outbox', 'cursors',
             'sessions', 'attempts', 'attempt_bindings', 'runtime_outbox', 'runtime_updates', 'runtime_status', 'session_history', 'daemon_meta', 'daemon_commands', 'daemon_attempts', 'daemon_sessions', 'daemon_updates'):
    documents[name] = sa.Table(
        name, metadata,
        sa.Column('key', sa.String(128), primary_key=True),
        sa.Column('body', sa.JSON(), nullable=False),
    )
