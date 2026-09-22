"""Add indexed Slack-thread lookup for workflow runs.

Revision ID: 36ad5285394c
Revises: sqlite_0008
Create Date: 2026-09-22 16:22:20.752698
"""

from alembic import op
import sqlalchemy as sa


revision = "36ad5285394c"
down_revision = "sqlite_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run_states") as batch:
        batch.add_column(sa.Column("origin_channel", sa.Text()))
        batch.add_column(sa.Column("origin_thread_id", sa.Text()))
        batch.create_index(
            "runs_by_origin_thread", ["origin_channel", "origin_thread_id"]
        )
    # Existing runs retain their Slack link when databases upgrade. The
    # adapter keeps the complete origin in state_json; these columns are the
    # indexed projection used to avoid deserialising unrelated WorkOrders.
    op.execute(
        """
        UPDATE run_states
        SET origin_channel = json_extract(state_json, '$.origin.channel'),
            origin_thread_id = json_extract(state_json, '$.origin.thread_id')
        WHERE json_valid(state_json)
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("run_states") as batch:
        batch.drop_index("runs_by_origin_thread")
        batch.drop_column("origin_thread_id")
        batch.drop_column("origin_channel")
