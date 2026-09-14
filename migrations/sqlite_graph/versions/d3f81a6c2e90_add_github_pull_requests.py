"""record which run opened a github pull request

Revision ID: d3f81a6c2e90
Revises: b41d9c0f5a3e
Create Date: 2026-09-11 09:12:44.118207
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3f81a6c2e90'
down_revision: Union[str, Sequence[str], None] = 'b41d9c0f5a3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Give pull-request ownership a row of its own.

    Ownership was read off `github_comments` by taking the newest comment on
    the pull request, which answers a different question: every run that
    comments is recorded there, so a review or a follow-up displaced the run
    that opened it. Opening is the act that creates ownership, so it gets the
    table, keyed by the pull request itself -- one owner, found by seek.

    Nothing is backfilled. The comment table cannot say who opened anything,
    and inventing an owner from it would write down the very guess this
    replaces; an unrecorded pull request answers `None`, which is also the
    honest answer for one opened by hand.
    """
    op.create_table(
        "github_pull_requests",
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text()),
        sa.Column("opened_at", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.PrimaryKeyConstraint("repository", "number"),
    )
    op.create_index(
        "github_pull_requests_by_run", "github_pull_requests", ["run_id"]
    )


def downgrade() -> None:
    op.drop_index("github_pull_requests_by_run", table_name="github_pull_requests")
    op.drop_table("github_pull_requests")
