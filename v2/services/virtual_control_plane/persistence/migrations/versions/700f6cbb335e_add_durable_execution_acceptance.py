"""add durable execution acceptance

Revision ID: 700f6cbb335e
Revises: 591de3f10346
Create Date: 2026-11-02 06:42:18.912710
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '700f6cbb335e'
down_revision: Union[str, Sequence[str], None] = '591de3f10346'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for name in ("sessions", "attempts", "attempt_bindings", "runtime_status"):
        op.create_table(name, sa.Column("key", sa.String(128), primary_key=True),
                        sa.Column("body", sa.JSON(), nullable=False))
    op.create_table("runtime_commands", sa.Column("command_id", sa.String(128), primary_key=True),
                    sa.Column("subject", sa.Text(), nullable=False), sa.Column("wire", sa.LargeBinary(), nullable=False))


def downgrade() -> None:
    op.drop_table("runtime_commands")
    for name in ("runtime_status", "attempt_bindings", "attempts", "sessions"):
        op.drop_table(name)
