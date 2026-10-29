"""scope graphs and command receipts to projects

Revision ID: d75ffe6cc5ed
Revises: 4925ef9f3b79
Create Date: 2026-10-28 10:09:27.357962

Graph names, operations' command IDs and command receipts become unique per
project rather than per GE. Every existing row moves to the `default`
project, keeping its graph ID and revisions.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd75ffe6cc5ed'
down_revision: Union[str, Sequence[str], None] = '4925ef9f3b79'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_PROJECT = 'default'


class RowsOutsideDefaultProject(RuntimeError):
    """A downgrade would merge projects whose graph names or command IDs may collide."""

    def __init__(self, table: str) -> None:
        super().__init__(f"{table} has rows outside the {DEFAULT_PROJECT} project; remove them to downgrade")


def _add_project(table: str) -> None:
    """Add `project` to `table`, filled with the default project for the rows it has."""
    with op.batch_alter_table(table) as batch_op:
        batch_op.add_column(sa.Column('project', sa.String(length=64), nullable=True))
    op.execute(sa.table(table, sa.column('project')).update().values(project=DEFAULT_PROJECT))


def _rebuild_receipts(key: list[str], *added: sa.ColumnElement) -> None:
    """Replace command_receipts with one keyed on `key`, copying its rows with the `added` columns."""
    columns = [
        sa.Column('command_id', sa.String(length=128), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    ]
    if 'project' in key:
        columns.insert(0, sa.Column('project', sa.String(length=64), nullable=False))
    names = [column.name for column in columns if column.name != 'project']
    rebuilt = op.create_table('command_receipts_rebuilt', *columns, sa.PrimaryKeyConstraint(*key))
    old = sa.table('command_receipts', *(sa.column(name) for name in names))
    op.execute(rebuilt.insert().from_select(
        [*(column.name for column in added), *names], sa.select(*added, *(old.c[name] for name in names))
    ))
    op.drop_table('command_receipts')
    op.rename_table('command_receipts_rebuilt', 'command_receipts')


def upgrade() -> None:
    _add_project('graphs')
    with op.batch_alter_table('graphs') as batch_op:
        batch_op.alter_column('project', existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_constraint('uq_graphs_name', type_='unique')
        batch_op.create_unique_constraint('uq_graphs_project_name', ['project', 'name'])

    _add_project('operations')
    with op.batch_alter_table('operations') as batch_op:
        batch_op.alter_column('project', existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_constraint('uq_operations_command_id', type_='unique')
        batch_op.create_unique_constraint('uq_operations_project_command_id', ['project', 'command_id'])

    # Its key gains `project`, so the table is rebuilt rather than altered.
    _rebuild_receipts(['project', 'command_id'], sa.literal(DEFAULT_PROJECT).label('project'))


def downgrade() -> None:
    # Names and command IDs are unique per GE again, so only one project's rows can stay.
    bind = op.get_bind()
    for table in ('graphs', 'operations', 'command_receipts'):
        if bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table} WHERE project != :default"), {'default': DEFAULT_PROJECT}):
            raise RowsOutsideDefaultProject(table)

    _rebuild_receipts(['command_id'])

    with op.batch_alter_table('operations') as batch_op:
        batch_op.drop_constraint('uq_operations_project_command_id', type_='unique')
        batch_op.create_unique_constraint('uq_operations_command_id', ['command_id'])
        batch_op.drop_column('project')

    with op.batch_alter_table('graphs') as batch_op:
        batch_op.drop_constraint('uq_graphs_project_name', type_='unique')
        batch_op.create_unique_constraint('uq_graphs_name', ['name'])
        batch_op.drop_column('project')
