"""store workorder metadata on runs

Revision ID: 3a749697aced
Revises: 17c1558d2a6b
Create Date: 2026-10-12 16:24:13.289988
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3a749697aced'
down_revision: Union[str, Sequence[str], None] = '17c1558d2a6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TEXT_COLUMNS = (
    "name", "prompt", "repository", "requester", "origin_channel", "origin_thread_id",
    "parent_run_id", "depends_on_run_id", "failure_reason", "created_at", "started_at", "task_id",
)


def upgrade() -> None:
    for name in _TEXT_COLUMNS:
        op.add_column("runs", sa.Column(name, sa.Text(), nullable=True))
    for name in ("inputs", "origin"):
        op.add_column("runs", sa.Column(name, sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("scheduled", sa.Boolean(), server_default="0", nullable=True))
    op.create_index("runs_by_origin", "runs", ["origin_channel", "origin_thread_id"])
    op.create_index("runs_by_created_at", "runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("runs_by_created_at", table_name="runs")
    op.drop_index("runs_by_origin", table_name="runs")
    with op.batch_alter_table("runs") as batch:
        for name in (*_TEXT_COLUMNS, "inputs", "origin", "scheduled"):
            batch.drop_column(name)
