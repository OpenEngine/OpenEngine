"""add durable execution acceptance

Revision ID: 6c4f1e3fd516
Revises: 7c6330e71418
Create Date: 2026-10-22 18:30:50.546869
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '6c4f1e3fd516'
down_revision: Union[str, Sequence[str], None] = '7c6330e71418'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for name in ('sessions', 'attempts', 'attempt_bindings', 'runtime_outbox'):
        op.create_table(name, sa.Column('key', sa.String(128), primary_key=True),
                        sa.Column('body', sa.JSON(), nullable=False))


def downgrade() -> None:
    for name in ('runtime_outbox', 'attempt_bindings', 'attempts', 'sessions'):
        op.drop_table(name)
