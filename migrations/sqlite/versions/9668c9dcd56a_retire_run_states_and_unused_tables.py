"""retire run states and unused tables

Revision ID: 9668c9dcd56a
Revises: d487944fbd15
Create Date: 2026-10-12 16:24:13.295818
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9668c9dcd56a'
down_revision: Union[str, Sequence[str], None] = 'd487944fbd15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The daemon opens/migrates state before the graph DB. Keep an immutable
    # handoff so standalone engine-migrate is safe too. Startup imports these
    # rows with null-only updates; this is no longer a live run index.
    op.create_table(
        "legacy_run_metadata",
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("requester", sa.Text()),
        sa.Column("origin_channel", sa.Text()),
        sa.Column("origin_thread_id", sa.Text()),
    )
    op.execute("INSERT INTO legacy_run_metadata SELECT sequence, run_id, state_json, requester, origin_channel, origin_thread_id FROM run_states")
    op.drop_table("run_events")
    op.drop_table("run_states")
    op.drop_table("projects")


def downgrade() -> None:
    raise NotImplementedError("Run metadata has moved to the graph database")
