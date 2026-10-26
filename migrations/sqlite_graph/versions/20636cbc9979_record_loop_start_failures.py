"""record loop start failures

Revision ID: 20636cbc9979
Revises: 3293d6fa005f
Create Date: 2026-10-24 13:32:29.982099
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20636cbc9979'
down_revision: Union[str, Sequence[str], None] = '3293d6fa005f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cli_loops", sa.Column("last_error", sa.Text(), nullable=False, server_default=""))
    op.add_column("cli_loops", sa.Column("last_error_at", sa.Text(), nullable=True))
    op.add_column("cli_loops", sa.Column(
        "consecutive_start_failures", sa.Integer(), nullable=False, server_default="0",
    ))


def downgrade() -> None:
    op.drop_column("cli_loops", "consecutive_start_failures")
    op.drop_column("cli_loops", "last_error_at")
    op.drop_column("cli_loops", "last_error")
