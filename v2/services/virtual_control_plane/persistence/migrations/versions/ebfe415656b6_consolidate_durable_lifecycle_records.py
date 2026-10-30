"""consolidate durable lifecycle records

Revision ID: ebfe415656b6
Revises: d8f92154b5f7
Create Date: 2026-10-30 04:52:53.437737
"""

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ebfe415656b6'
down_revision: Union[str, Sequence[str], None] = 'd8f92154b5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _prototype_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=True),
    )


def _prototype(name: str) -> sa.Table:
    return sa.Table(name, sa.MetaData(), *_prototype_columns())


def _documents(name: str) -> sa.Table:
    return sa.table(name, sa.column("key", sa.String(128)), sa.column("body", sa.JSON()))


def upgrade() -> None:
    connection = op.get_bind()
    op.rename_table("workspaces", "_prototype_workspaces")
    for name in ("workspaces", "workspace_runtimes", "operations", "command_receipts"):
        op.create_table(
            name,
            sa.Column("key", sa.String(128), primary_key=True),
            sa.Column("body", sa.JSON(), nullable=False),
        )
    # Preserve opaque locators and timestamps, but create no lifecycle projection
    # or runtime ownership. Only the prototype adapter interprets legacy bodies.
    for row in connection.execute(sa.select(_prototype("_prototype_workspaces"))).mappings():
        body = dict(row)
        for name in ("created_at", "updated_at"):
            body[name] = body[name].replace(tzinfo=timezone.utc).isoformat()
        connection.execute(_documents("workspaces").insert().values(key=row["id"], body={"legacy": body}))
    op.drop_table("_prototype_workspaces")


def downgrade() -> None:
    connection = op.get_bind()
    op.rename_table("workspaces", "_workspace_documents")
    prototype = _prototype("workspaces")
    op.create_table("workspaces", *_prototype_columns())
    op.create_index("ix_workspaces_status", "workspaces", ["status"])
    for row in connection.execute(sa.select(_documents("_workspace_documents"))).mappings():
        if "legacy" not in row["body"]:
            continue
        body = dict(row["body"]["legacy"])
        for name in ("created_at", "updated_at"):
            body[name] = datetime.fromisoformat(body[name])
        connection.execute(prototype.insert().values(**body))
    op.drop_table("_workspace_documents")
    for name in ("command_receipts", "operations", "workspace_runtimes"):
        op.drop_table(name)
