"""add durable control plane records

Revision ID: 7c6330e71418
Revises: d8f92154b5f7
Create Date: 2026-10-22 16:40:05.348937
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c6330e71418'
down_revision: Union[str, Sequence[str], None] = 'd8f92154b5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for name in ('workspace_records', 'command_receipts', 'operations', 'runtime_ownership', 'outbox', 'cursors'):
        op.create_table(name, sa.Column('key', sa.String(128), primary_key=True), sa.Column('body', sa.JSON(), nullable=False))


def downgrade() -> None:
    for name in ('cursors', 'outbox', 'runtime_ownership', 'operations', 'command_receipts', 'workspace_records'):
        op.drop_table(name)
