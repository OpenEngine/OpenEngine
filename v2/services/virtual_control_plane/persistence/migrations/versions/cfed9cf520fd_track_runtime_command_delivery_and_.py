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
        batch.add_column(sa.Column("attempt_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("acknowledgement", sa.LargeBinary(), nullable=True))
    # Every accepted command names its attempt in the bytes it was accepted with.
    op.execute("UPDATE runtime_commands SET attempt_id = json_extract(CAST(wire AS TEXT), '$.target.attempt_id')")
    with op.batch_alter_table("runtime_commands") as batch:
        batch.alter_column("attempt_id", existing_type=sa.String(128), nullable=False)
        batch.create_foreign_key("fk_runtime_commands_attempt_id_attempts", "attempts", ["attempt_id"], ["key"])
        batch.create_check_constraint("ck_runtime_commands_status",
                                      "status IN ('pending', 'acknowledged', 'rejected', 'blocked')")
    op.create_index("ix_runtime_commands_pending", "runtime_commands", ["status"],
                    sqlite_where=sa.text("status = 'pending'"))
    op.create_table(
        "runtime_updates",
        sa.Column("runtime_id", sa.String(128), sa.ForeignKey("runtime_status.key"), primary_key=True),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("runtime_id", "sequence", name="uq_runtime_updates_sequence"),
        comment="Committed internal runtime updates, for duplicate and conflict detection.",
    )


def downgrade() -> None:
    op.drop_table("runtime_updates")
    op.drop_index("ix_runtime_commands_pending", table_name="runtime_commands")
    with op.batch_alter_table("runtime_commands") as batch:
        batch.drop_constraint("ck_runtime_commands_status", type_="check")
        batch.drop_constraint("fk_runtime_commands_attempt_id_attempts", type_="foreignkey")
        batch.drop_column("acknowledgement")
        batch.drop_column("status")
        batch.drop_column("attempt_id")
