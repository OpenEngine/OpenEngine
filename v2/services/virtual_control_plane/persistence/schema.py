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
