"""add node runner overrides

Revision ID: 26a6a404b5fe
Revises: sqlite_graph_0001
Create Date: 2026-09-09 12:23:20.339844
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '26a6a404b5fe'
down_revision: Union[str, Sequence[str], None] = 'sqlite_graph_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("runner_overrides", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("runs", "runner_overrides")
