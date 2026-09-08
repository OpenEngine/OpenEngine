"""Add workstream scope.

Revision ID: sqlite_0004
Revises: sqlite_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "sqlite_0004"
down_revision = "sqlite_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workstreams") as batch:
        batch.add_column(
            sa.Column("scope", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("workstreams") as batch:
        batch.drop_column("scope")
