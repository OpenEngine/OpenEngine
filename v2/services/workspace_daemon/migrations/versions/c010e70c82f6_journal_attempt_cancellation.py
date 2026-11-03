"""journal attempt cancellation

Revision ID: c010e70c82f6
Revises: 2b6e990cf980
Create Date: 2026-11-03 02:27:26.722235
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c010e70c82f6'
down_revision: Union[str, Sequence[str], None] = '2b6e990cf980'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Autogenerate does not compare CHECK constraints: the state choices are rewritten here.
STATES = "state IN ('recorded', 'launching', 'running', 'completed', 'failed', 'cancelled')"
CANCELLING = "state IN ('recorded', 'launching', 'running', 'cancelling', 'completed', 'failed', 'cancelled')"


def upgrade() -> None:
    with op.batch_alter_table('journal_attempts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancel_command_id', sa.String(length=128), nullable=True))
        batch_op.alter_column('session_id',
               existing_type=sa.VARCHAR(length=128),
               nullable=True)
        batch_op.create_foreign_key('fk_journal_attempts_cancel_command_id_journal_commands', 'journal_commands',
                                    ['cancel_command_id'], ['command_id'])
        batch_op.drop_constraint('ck_journal_attempts_state', type_='check')
        batch_op.create_check_constraint('ck_journal_attempts_state', sa.text(CANCELLING))


def downgrade() -> None:
    # The previous schema has neither tombstones nor cancel intents. A tombstone (an attempt
    # without a session) is dropped while its journaled updates are kept; a cancelling attempt
    # becomes running, which that daemon fences on start and records lost, never relaunching it.
    op.execute("UPDATE journal_updates SET attempt_id = NULL WHERE attempt_id IN "
               "(SELECT attempt_id FROM journal_attempts WHERE session_id IS NULL)")
    op.execute("DELETE FROM journal_attempts WHERE session_id IS NULL")
    op.execute("UPDATE journal_attempts SET state = 'running' WHERE state = 'cancelling'")
    with op.batch_alter_table('journal_attempts', schema=None) as batch_op:
        batch_op.drop_constraint('ck_journal_attempts_state', type_='check')
        batch_op.create_check_constraint('ck_journal_attempts_state', sa.text(STATES))
        batch_op.drop_constraint('fk_journal_attempts_cancel_command_id_journal_commands', type_='foreignkey')
        batch_op.alter_column('session_id',
               existing_type=sa.VARCHAR(length=128),
               nullable=False)
        batch_op.drop_column('cancel_command_id')
