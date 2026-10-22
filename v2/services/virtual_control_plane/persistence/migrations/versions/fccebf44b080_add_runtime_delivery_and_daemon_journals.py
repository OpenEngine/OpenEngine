"""add runtime delivery and daemon journals

Revision ID: fccebf44b080
Revises: 6c4f1e3fd516
Create Date: 2026-10-22 20:01:45.050655
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fccebf44b080'
down_revision: Union[str, Sequence[str], None] = '6c4f1e3fd516'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NAMES = ('runtime_updates', 'runtime_status', 'session_history', 'daemon_meta', 'daemon_commands', 'daemon_attempts', 'daemon_sessions', 'daemon_updates')


def upgrade() -> None:
    for name in NAMES:
        op.create_table(name, sa.Column('key', sa.String(128), primary_key=True),
                        sa.Column('body', sa.JSON(), nullable=False))


def downgrade() -> None:
    for name in reversed(NAMES):
        op.drop_table(name)
