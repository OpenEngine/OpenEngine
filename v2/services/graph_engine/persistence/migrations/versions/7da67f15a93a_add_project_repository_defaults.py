"""add project repository defaults

Revision ID: 7da67f15a93a
Revises: 5cce010cee3d
Create Date: 2026-11-02 13:30:50.820101
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7da67f15a93a'
down_revision: Union[str, Sequence[str], None] = '5cce010cee3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("tenant_id", sa.CHAR(32), nullable=False),
        sa.Column("project", sa.String(64), nullable=False),
        sa.Column("checkout", sa.Text, nullable=False),
        sa.Column("base_ref", sa.String(256), nullable=False),
        sa.Column("workspace_policy", sa.String(32), nullable=False),
        sa.Column("trusted", sa.Boolean, nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "project"),
        comment="One explicitly registered local checkout and execution defaults per project.",
    )


def downgrade() -> None:
    op.drop_table("repositories")
