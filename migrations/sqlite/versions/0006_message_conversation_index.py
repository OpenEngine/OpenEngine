"""Index messages by conversation and sequence.

Revision ID: sqlite_0006
Revises: sqlite_0005
"""

from alembic import op


revision = "sqlite_0006"
down_revision = "sqlite_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "messages_by_instance",
        "messages",
        ["instance_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("messages_by_instance", table_name="messages")
