"""tenant scope tool persistence

Revision ID: 1f78d3a45137
Revises: 48be4be506f8
Create Date: 2026-11-02 07:11:06.743397
"""

from typing import Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa


revision: str = '1f78d3a45137'
down_revision: Union[str, Sequence[str], None] = '48be4be506f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



LOCAL_TENANT = '00000000-0000-7000-8000-000000000001'
TABLES = ('tools', 'tool_grants', 'tool_invocations', 'policies',
          'tool_invocation_attempts')
NAMING = {
    'pk': 'pk_%(table_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
}
ATTEMPTS = 'tool_invocation_attempts'
FK = 'fk_tool_invocation_attempts_invocation_uid_tool_invocations'


def _detach_attempts() -> None:
    # Detach before rebuilding the parent: with SQLite FK enforcement enabled,
    # dropping the old parent would otherwise cascade-delete existing attempts.
    foreign_key, = sa.inspect(op.get_bind()).get_foreign_keys(ATTEMPTS)
    with op.batch_alter_table(ATTEMPTS, naming_convention=NAMING) as batch:
        batch.drop_constraint(foreign_key['name'] or FK, type_='foreignkey')


def _attach_attempts(scoped: bool) -> None:
    columns = ['tenant_id', 'invocation_uid'] if scoped else ['invocation_uid']
    targets = ['tenant_id', 'uid'] if scoped else ['uid']
    with op.batch_alter_table(ATTEMPTS, naming_convention=NAMING) as batch:
        batch.create_foreign_key(FK, 'tool_invocations', columns, targets,
                                 ondelete='CASCADE')


def _keys(table: str, scoped: bool) -> None:
    key = ['invocation_uid', 'number'] if table == ATTEMPTS else ['uid']
    primary_key = sa.inspect(op.get_bind()).get_pk_constraint(table)
    with op.batch_alter_table(table, naming_convention=NAMING) as batch:
        batch.drop_constraint(primary_key['name'] or f'pk_{table}', type_='primary')
        batch.create_primary_key(f'pk_{table}', ['tenant_id', *key] if scoped else key)
        if table != ATTEMPTS:
            unique = f'uq_{table}_namespace_name'
            batch.drop_constraint(unique, type_='unique')
            batch.create_unique_constraint(
                unique, ['tenant_id', 'namespace', 'name'] if scoped else ['namespace', 'name'])
        if scoped:
            batch.alter_column('tenant_id', existing_type=sa.Uuid(), server_default=None)
        else:
            batch.drop_column('tenant_id')


def upgrade() -> None:
    _detach_attempts()
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(
                'tenant_id', sa.Uuid(), nullable=False,
                server_default=sa.literal(LOCAL_TENANT.replace('-', ''))))
        _keys(table, True)
    _attach_attempts(True)


def downgrade() -> None:
    # Check every table before any DDL, even if tenants have no colliding keys.
    bind = op.get_bind()
    for name in TABLES:
        table = sa.table(name, sa.column('tenant_id', sa.Uuid()))
        if bind.scalar(sa.select(sa.func.count()).select_from(table).where(
                table.c.tenant_id != UUID(LOCAL_TENANT))):
            message = f'{name} has rows outside LOCAL_TENANT; remove them to downgrade'
            raise RuntimeError(message)
    _detach_attempts()
    for table in TABLES:
        _keys(table, False)
    _attach_attempts(False)
