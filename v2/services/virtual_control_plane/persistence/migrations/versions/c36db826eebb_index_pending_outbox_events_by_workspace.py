"""index pending outbox events by workspace

Revision ID: c36db826eebb
Revises: 09a0811b905f
Create Date: 2026-10-30 18:51:23.226091
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c36db826eebb'
down_revision: Union[str, Sequence[str], None] = '09a0811b905f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_outbox_workspace_pending", "outbox", ["workspace_id", "sequence"],
        sqlite_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_workspace_pending", table_name="outbox")
