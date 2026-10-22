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

# Durable documents are deliberately separate from the original prototype
# workspace table. Indexed identities and transaction boundaries are stable;
# document bodies are the application snapshots, not SQLAlchemy domain objects.
DOCUMENT_COMMENTS = {
    'workspace_records': "The durable workspace lifecycle projection: state, runtime binding and version.",
    'command_receipts': 'Public command idempotency: a retry with the same command ID returns the original receipt.',
    'operations': "The durable record of each accepted command's progress, returned by operations.{operation_id}.get.",
    'runtime_ownership': 'Which runtime currently owns a workspace, and at which generation.',
    'outbox': 'Public events accepted atomically with their causing state change, pending publication.',
    'cursors': 'Durable, opaque keyset positions bound to the filters a paginated query was issued with.',
    'sessions': 'One conversation with an agent inside a workspace, and its current attempt binding.',
    'attempts': 'One execution attempt of a session, from acceptance through its terminal outcome.',
    'attempt_bindings': "An attempt's immutable binding to the internal execute command the daemon accepted.",
    'runtime_outbox': 'Internal runtime commands (execute, cancel, steer) pending delivery to the daemon.',
    'runtime_updates': 'Durable identity/digest of each internal runtime update, so a duplicate is detected, not reapplied.',
    'runtime_status': "A runtime's last known readiness, registered executors and steering support.",
    'session_history': 'The durable, ordered log of a session: messages, steering and status changes.',
    'daemon_meta': "The daemon journal's own metadata: boot ID, event sequence and readiness.",
    'daemon_commands': "The daemon's durable inbox of accepted commands, keyed by their command ID.",
    'daemon_attempts': "The daemon's durable record of one attempt's execution state and per-attempt sequence.",
    'daemon_sessions': "The daemon's durable session-to-executor mapping, for session continuation.",
    'daemon_updates': "The daemon's durable outbox of updates pending VCP's acknowledgement.",
}
documents = {}
for name, comment in DOCUMENT_COMMENTS.items():
    documents[name] = sa.Table(
        name, metadata,
        sa.Column('key', sa.String(128), primary_key=True),
        sa.Column('body', sa.JSON(), nullable=False),
        comment=comment,
    )
