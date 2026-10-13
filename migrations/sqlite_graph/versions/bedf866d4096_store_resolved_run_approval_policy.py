"""store resolved run approval policy

Revision ID: bedf866d4096
Revises: c3836c457cf8
Create Date: 2026-10-12 19:19:28.634778
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bedf866d4096'
down_revision: Union[str, Sequence[str], None] = 'c3836c457cf8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("approval_policy", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("runs", "approval_policy")
