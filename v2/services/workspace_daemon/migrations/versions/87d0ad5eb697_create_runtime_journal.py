"""create runtime journal

Revision ID: 87d0ad5eb697
Revises: 
Create Date: 2026-11-02 09:54:30.383553
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '87d0ad5eb697'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('journal_commands',
    sa.Column('command_id', sa.String(length=128), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('command', sa.LargeBinary(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.CheckConstraint("(status = 'accepted') = (error_code IS NULL)", name='ck_journal_commands_error'),
    sa.CheckConstraint("status IN ('accepted', 'rejected')", name='ck_journal_commands_status'),
    sa.PrimaryKeyConstraint('command_id'),
    comment='Remembered execute commands as received and their final reply.'
    )
    op.create_table('journal_sessions',
    sa.Column('session_id', sa.String(length=128), nullable=False),
    sa.Column('command_id', sa.String(length=128), nullable=False),
    sa.ForeignKeyConstraint(['command_id'], ['journal_commands.command_id'], ),
    sa.PrimaryKeyConstraint('session_id'),
    comment='Sessions owned by this runtime; the opening command carries their agent.'
    )
    op.create_table('journal_attempts',
    sa.Column('attempt_id', sa.String(length=128), nullable=False),
    sa.Column('session_id', sa.String(length=128), nullable=False),
    sa.Column('command_id', sa.String(length=128), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('launch_count', sa.Integer(), nullable=False),
    sa.Column('attempt_sequence', sa.Integer(), nullable=False),
    sa.Column('last_event_sequence', sa.Integer(), nullable=False),
    sa.Column('executor_handle', sa.JSON(), nullable=True),
    sa.CheckConstraint("state IN ('recorded', 'launching', 'running', 'completed', 'failed', 'cancelled')", name='ck_journal_attempts_state'),
    sa.ForeignKeyConstraint(['command_id'], ['journal_commands.command_id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['journal_sessions.session_id'], ),
    sa.PrimaryKeyConstraint('attempt_id'),
    sa.UniqueConstraint('command_id'),
    comment='Accepted attempts and their execution progress.'
    )
    with op.batch_alter_table('journal_attempts', schema=None) as batch_op:
        batch_op.create_index('ix_journal_attempts_state', ['state'], unique=False, sqlite_where=sa.text("state NOT IN ('completed', 'failed', 'cancelled')"))
        batch_op.create_index('ux_journal_attempts_active_session', ['session_id'], unique=True, sqlite_where=sa.text("state NOT IN ('completed', 'failed', 'cancelled')"))

    op.create_table('journal_updates',
    sa.Column('sequence', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('event_id', sa.String(length=128), nullable=False),
    sa.Column('attempt_id', sa.String(length=128), nullable=True),
    sa.Column('wire', sa.LargeBinary(), nullable=False),
    sa.Column('acknowledged', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['attempt_id'], ['journal_attempts.attempt_id'], ),
    sa.PrimaryKeyConstraint('sequence'),
    comment='Ordered runtime updates as sent, retained after VCP commits them.'
    )
    with op.batch_alter_table('journal_updates', schema=None) as batch_op:
        batch_op.create_index('ix_journal_updates_event_id', ['event_id'], unique=True)
        batch_op.create_index('ix_journal_updates_pending', ['sequence'], unique=False, sqlite_where=sa.text('acknowledged = 0'))

    op.create_table('journal_runtime',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('workspace_id', sa.String(length=128), nullable=False),
    sa.Column('runtime_id', sa.String(length=128), nullable=False),
    sa.Column('runtime_generation', sa.Integer(), nullable=False),
    sa.Column('journal_id', sa.String(length=128), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('ready', sa.Boolean(), nullable=False),
    sa.Column('ready_sequence', sa.Integer(), nullable=True),
    sa.Column('blocked_code', sa.String(length=64), nullable=True),
    sa.Column('blocked_sequence', sa.Integer(), nullable=True),
    sa.Column('blocked_event_id', sa.String(length=128), nullable=True),
    sa.CheckConstraint('(blocked_code IS NULL) = (blocked_sequence IS NULL)', name='ck_journal_runtime_blocked'),
    sa.CheckConstraint('id = 1', name='ck_journal_runtime_single'),
    sa.ForeignKeyConstraint(['blocked_sequence'], ['journal_updates.sequence'], ),
    sa.ForeignKeyConstraint(['ready_sequence'], ['journal_updates.sequence'], ),
    sa.PrimaryKeyConstraint('id'),
    comment='The single provisioned binding, journal identity, update high-water mark and readiness.'
    )


def downgrade() -> None:
    op.drop_table('journal_runtime')
    with op.batch_alter_table('journal_updates', schema=None) as batch_op:
        batch_op.drop_index('ix_journal_updates_pending', sqlite_where=sa.text('acknowledged = 0'))
        batch_op.drop_index('ix_journal_updates_event_id')

    op.drop_table('journal_updates')
    with op.batch_alter_table('journal_attempts', schema=None) as batch_op:
        batch_op.drop_index('ux_journal_attempts_active_session', sqlite_where=sa.text("state NOT IN ('completed', 'failed', 'cancelled')"))
        batch_op.drop_index('ix_journal_attempts_state', sqlite_where=sa.text("state NOT IN ('completed', 'failed', 'cancelled')"))

    op.drop_table('journal_attempts')
    op.drop_table('journal_sessions')
    op.drop_table('journal_commands')
