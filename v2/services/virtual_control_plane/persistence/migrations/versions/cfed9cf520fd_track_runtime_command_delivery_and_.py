"""track runtime command delivery and updates

Revision ID: cfed9cf520fd
Revises: 9b7f1ac760d9
Create Date: 2026-11-02 08:40:26.214986
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'cfed9cf520fd'
down_revision: Union[str, Sequence[str], None] = '9b7f1ac760d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Delivery state lives beside, never inside, the immutable command bytes.
    with op.batch_alter_table("runtime_commands") as batch:
        batch.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("acknowledgement", sa.LargeBinary(), nullable=True))
    op.create_index("ix_runtime_commands_pending", "runtime_commands", ["status"],
                    sqlite_where=sa.text("status = 'pending'"))
    op.create_table("runtime_updates", sa.Column("key", sa.String(128), primary_key=True),
                    sa.Column("body", sa.JSON(), nullable=False))


def downgrade() -> None:
    op.drop_table("runtime_updates")
    op.drop_index("ix_runtime_commands_pending", table_name="runtime_commands")
    with op.batch_alter_table("runtime_commands") as batch:
        batch.drop_column("acknowledgement")
        batch.drop_column("status")
