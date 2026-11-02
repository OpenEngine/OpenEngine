"""persist executor sessions

Revision ID: 2b6e990cf980
Revises: 87d0ad5eb697
Create Date: 2026-11-02 14:10:46.671816
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2b6e990cf980'
down_revision: Union[str, Sequence[str], None] = '87d0ad5eb697'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('journal_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('executor_session_id', sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column('executor_resumable', sa.Boolean(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('journal_sessions', schema=None) as batch_op:
        batch_op.drop_column('executor_resumable')
        batch_op.drop_column('executor_session_id')
