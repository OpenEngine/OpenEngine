"""scope GE tables to tenants

Revision ID: 5cce010cee3d
Revises: d75ffe6cc5ed
Create Date: 2026-11-02 12:35:26.159550
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5cce010cee3d'
down_revision: Union[str, Sequence[str], None] = 'd75ffe6cc5ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# These are storage types, deliberately independent of service metadata.
LOCAL_TENANT = '00000000000070008000000000000001'
TABLES = ('graphs', 'graph_revisions', 'operations', 'command_receipts')


class UnsafeTenantDowngrade(RuntimeError):
    """Tenant rows would lose their isolation on downgrade."""

    def __init__(self, table: str) -> None:
        super().__init__(f'{table} has non-local tenant rows; remove them to downgrade')


def _rebuild(scoped: bool) -> None:
    bind = op.get_bind()
    old = sa.MetaData()
    old.reflect(bind, only=TABLES)
    rebuilt = sa.MetaData()
    for name in TABLES:
        source = old.tables[name]
        columns = [sa.Column(c.name, c.type, nullable=c.nullable) for c in source.columns if c.name != 'tenant_id']
        if scoped:
            columns.insert(0, sa.Column('tenant_id', sa.CHAR(32), nullable=False))
        prefix = ['tenant_id'] if scoped else []
        primary = prefix + [c.name for c in source.primary_key.columns if c.name != 'tenant_id']
        constraints = [sa.PrimaryKeyConstraint(*primary)]
        for unique in source.constraints:
            if isinstance(unique, sa.UniqueConstraint):
                constraints.append(sa.UniqueConstraint(
                    *prefix, *(c.name for c in unique.columns if c.name != 'tenant_id'), name=unique.name,
                ))
        if name == 'graph_revisions':
            constraints.append(sa.ForeignKeyConstraint(
                prefix + ['graph_id'], [f'graphs_rebuilt.{c}' for c in prefix + ['graph_id']],
            ))
        table = sa.Table(name + '_rebuilt', rebuilt, *columns, *constraints)
        table.create(bind)
        values = [sa.literal(LOCAL_TENANT) if c.name == 'tenant_id' else source.c[c.name] for c in table.columns]
        op.execute(table.insert().from_select([c.name for c in table.columns], sa.select(*values)))
    # Drop children before parents, preserving populated revisions even with FK enforcement on.
    for name in ('graph_revisions', 'command_receipts', 'operations', 'graphs'):
        op.drop_table(name)
    for name in TABLES:
        op.rename_table(name + '_rebuilt', name)


def upgrade() -> None:
    _rebuild(True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        if bind.scalar(sa.text(f'SELECT COUNT(*) FROM {name} WHERE tenant_id != :local'), {'local': LOCAL_TENANT}):
            raise UnsafeTenantDowngrade(name)
    _rebuild(False)
