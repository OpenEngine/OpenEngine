"""Add milestone descriptions and dependencies.

Revision ID: sqlite_0002
Revises: sqlite_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "sqlite_0002"
down_revision = "sqlite_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("milestones") as batch:
        batch.add_column(
            sa.Column("description", sa.Text(), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("dependencies", sa.Text(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("milestones") as batch:
        batch.drop_column("dependencies")
        batch.drop_column("description")
