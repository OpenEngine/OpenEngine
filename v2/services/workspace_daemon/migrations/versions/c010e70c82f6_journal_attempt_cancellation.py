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


class UnsafeDowngrade(RuntimeError):
    def __init__(self, count: int) -> None:
        super().__init__(f"cannot downgrade: {count} journaled cancellation(s) would be lost and "
                         "cancelled work could launch")


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
    # The previous schema can represent neither a cancel-before-execute tombstone nor a cancel
    # intent. Dropping either would let a late execute launch cancelled work, so a journal that
    # holds one is refused rather than rewritten; an empty or cancellation-free journal downgrades.
    unsafe = op.get_bind().exec_driver_sql(
        "SELECT count(*) FROM journal_attempts WHERE session_id IS NULL OR cancel_command_id IS NOT NULL"
        " OR state = 'cancelling'").scalar()
    if unsafe:
        raise UnsafeDowngrade(unsafe)
    with op.batch_alter_table('journal_attempts', schema=None) as batch_op:
        batch_op.drop_constraint('ck_journal_attempts_state', type_='check')
        batch_op.create_check_constraint('ck_journal_attempts_state', sa.text(STATES))
        batch_op.drop_constraint('fk_journal_attempts_cancel_command_id_journal_commands', type_='foreignkey')
        batch_op.alter_column('session_id',
               existing_type=sa.VARCHAR(length=128),
               nullable=False)
        batch_op.drop_column('cancel_command_id')
