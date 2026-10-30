"""add workspace event outbox

Revision ID: 09a0811b905f
Revises: ebfe415656b6
Create Date: 2026-10-30 06:35:21.633165
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '09a0811b905f'
down_revision: Union[str, Sequence[str], None] = 'ebfe415656b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing lifecycle tables are untouched. AUTOINCREMENT never reuses a
    # sequence, so publication order survives any later deletion.
    op.create_table(
        "outbox",
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_outbox_event_id", "outbox", ["event_id"], unique=True)
    op.create_index("ix_outbox_pending", "outbox", ["sequence"], sqlite_where=sa.text("published_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_index("ix_outbox_event_id", table_name="outbox")
    op.drop_table("outbox")
