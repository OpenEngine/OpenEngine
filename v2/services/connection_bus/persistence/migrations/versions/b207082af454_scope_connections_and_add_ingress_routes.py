"""scope connections and add ingress routes

Revision ID: b207082af454
Revises: 0a34e647834f
Create Date: 2026-11-02 14:22:48.374822
"""

import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b207082af454'
down_revision: Union[str, Sequence[str], None] = '0a34e647834f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backfill before making ownership mandatory. No default remains for future writes.
    op.add_column("connections", sa.Column("tenant_id", sa.Uuid(), nullable=True))
    connections = sa.table("connections", sa.column("tenant_id", sa.Uuid()),
                           sa.column("provider", sa.String()), sa.column("external_id", sa.String()))
    op.execute(connections.update().values(tenant_id=uuid.UUID("00000000-0000-7000-8000-000000000001")))
    primary_key = sa.inspect(op.get_bind()).get_pk_constraint("connections")["name"] or "pk_connections"
    # SQLite's original primary key is unnamed; give reflection a deterministic name.
    with op.batch_alter_table("connections", naming_convention={"pk": "pk_%(table_name)s"}) as batch:
        batch.alter_column("tenant_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_constraint(primary_key, type_="primary")
        batch.create_primary_key("pk_connections", ["tenant_id", "id"])
        batch.drop_constraint("uq_connections_provider_external_id", type_="unique")
        batch.create_unique_constraint("uq_connections_provider_external_id", ["tenant_id", "provider", "external_id"])
    routes = op.create_table(
        "connection_routes",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("external_id", sa.String(255), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
    )
    op.execute(routes.insert().from_select(
        ["provider", "external_id", "tenant_id"],
        sa.select(connections.c.provider, connections.c.external_id, connections.c.tenant_id),
    ))


def downgrade() -> None:
    # Global route uniqueness guarantees restoring the original pair constraint is safe.
    op.drop_table("connection_routes")
    with op.batch_alter_table("connections") as batch:
        batch.drop_constraint("pk_connections", type_="primary")
        batch.create_primary_key("pk_connections", ["id"])
        batch.drop_constraint("uq_connections_provider_external_id", type_="unique")
        batch.create_unique_constraint("uq_connections_provider_external_id", ["provider", "external_id"])
        batch.drop_column("tenant_id")
