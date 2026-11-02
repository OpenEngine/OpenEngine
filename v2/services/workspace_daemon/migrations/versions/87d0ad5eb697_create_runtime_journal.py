"""create runtime journal

Revision ID: 87d0ad5eb697
Revises: 
Create Date: 2026-11-02 08:45:43.927575
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '87d0ad5eb697'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('journal_documents',
    sa.Column('collection', sa.String(length=32), nullable=False),
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('body', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('collection', 'key'),
    comment='Runtime binding, command receipts, attempts and session ownership.'
    )
    op.create_table('journal_updates',
    sa.Column('sequence', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('event_id', sa.String(length=128), nullable=False),
    sa.Column('wire', sa.LargeBinary(), nullable=False),
    sa.Column('acknowledged', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.PrimaryKeyConstraint('sequence'),
    comment='Ordered runtime updates as sent, retained after VCP commits them.'
    )
    with op.batch_alter_table('journal_updates', schema=None) as batch_op:
        batch_op.create_index('ix_journal_updates_event_id', ['event_id'], unique=True)
        batch_op.create_index('ix_journal_updates_pending', ['sequence'], unique=False, sqlite_where=sa.text('acknowledged = 0'))


def downgrade() -> None:
    with op.batch_alter_table('journal_updates', schema=None) as batch_op:
        batch_op.drop_index('ix_journal_updates_pending', sqlite_where=sa.text('acknowledged = 0'))
        batch_op.drop_index('ix_journal_updates_event_id')

    op.drop_table('journal_updates')
    op.drop_table('journal_documents')
