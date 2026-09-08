"""Add project archive state.

Revision ID: sqlite_0003
Revises: sqlite_0002
"""

from alembic import op
import sqlalchemy as sa


revision = "sqlite_0003"
down_revision = "sqlite_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("archived", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("archived")
