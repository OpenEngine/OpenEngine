"""add github comments

Revision ID: c7c9f42f4747
Revises: 26a6a404b5fe
Create Date: 2026-09-10 12:41:45.626376
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7c9f42f4747'
down_revision: Union[str, Sequence[str], None] = '26a6a404b5fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "github_comments",
        sa.Column("comment_id", sa.Integer(), primary_key=True),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text()),
        sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
    )
    op.create_index("github_comments_by_run", "github_comments", ["run_id"])
    op.create_index("github_comments_by_pr", "github_comments", ["pr_number"])


def downgrade() -> None:
    op.drop_index("github_comments_by_pr", table_name="github_comments")
    op.drop_index("github_comments_by_run", table_name="github_comments")
    op.drop_table("github_comments")
