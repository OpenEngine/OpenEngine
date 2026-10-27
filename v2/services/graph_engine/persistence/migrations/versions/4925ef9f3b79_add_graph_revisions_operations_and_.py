"""add graph revisions, operations and command receipts

Revision ID: 4925ef9f3b79
Revises: b552e26457cd
Create Date: 2026-10-26 20:11:15.586658

`graphs` becomes one row per graph under a stable ID, and its versions move to
`graph_revisions`. Nothing wrote the old table, so it is replaced, not converted.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4925ef9f3b79'
down_revision: Union[str, Sequence[str], None] = 'b552e26457cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('graphs')
    op.create_table('graphs',
    sa.Column('graph_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('description', sa.String(length=2000), nullable=True),
    sa.Column('latest_revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('graph_id'),
    sa.UniqueConstraint('name', name='uq_graphs_name')
    )
    op.create_table('graph_revisions',
    sa.Column('graph_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('format', sa.String(length=16), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('source_sha256', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['graph_id'], ['graphs.graph_id'], ),
    sa.PrimaryKeyConstraint('graph_id', 'revision')
    )
    op.create_table('operations',
    sa.Column('operation_id', sa.String(length=128), nullable=False),
    sa.Column('command_id', sa.String(length=128), nullable=False),
    sa.Column('operation', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('resources', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('error', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('operation_id'),
    sa.UniqueConstraint('command_id', name='uq_operations_command_id')
    )
    op.create_table('command_receipts',
    sa.Column('command_id', sa.String(length=128), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('response', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('command_id')
    )


def downgrade() -> None:
    op.drop_table('command_receipts')
    op.drop_table('operations')
    op.drop_table('graph_revisions')
    op.drop_table('graphs')
    op.create_table('graphs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('definition', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', 'version', name='uq_graphs_name_version')
    )
