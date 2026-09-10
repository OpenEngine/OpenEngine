"""widen the github comment key

Revision ID: b41d9c0f5a3e
Revises: c7c9f42f4747
Create Date: 2026-09-10 19:20:11.481203
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b41d9c0f5a3e'
down_revision: Union[str, Sequence[str], None] = 'c7c9f42f4747'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Name a comment by its repository and id space as well as its id.

    A bare `comment_id` is not a name: GitHub numbers conversation comments
    and inline review comments from separate sequences, and every repository
    has its own #1 of each, so two unrelated comments could claim one row and
    the first would disappear without an error. The table is recreated rather
    than altered because SQLite cannot restate a primary key, and dropping it
    loses nothing: nothing wrote to it before this revision.
    """
    op.drop_table("github_comments")
    op.create_table(
        "github_comments",
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text()),
        sa.Column("posted_at", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.PrimaryKeyConstraint("repository", "kind", "comment_id"),
    )
    op.create_index("github_comments_by_run", "github_comments", ["run_id"])
    op.create_index(
        "github_comments_by_pr", "github_comments", ["repository", "pr_number"]
    )


def downgrade() -> None:
    op.drop_index("github_comments_by_pr", table_name="github_comments")
    op.drop_index("github_comments_by_run", table_name="github_comments")
    op.drop_table("github_comments")
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
