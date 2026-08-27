"""Add the model a conversation runs on.

Revision ID: sqlite_0005
Revises: sqlite_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "sqlite_0005"
down_revision = "sqlite_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_instances") as batch:
        batch.add_column(
            sa.Column("model", sa.Text(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_instances") as batch:
        batch.drop_column("model")
