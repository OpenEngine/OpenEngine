"""add run forge context

Revision ID: 17c1558d2a6b
Revises: 90163fc5ff5b
Create Date: 2026-10-12 14:42:30.139799
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '17c1558d2a6b'
down_revision: Union[str, Sequence[str], None] = '90163fc5ff5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("forge_context", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("runs", "forge_context")
