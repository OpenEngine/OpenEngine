"""index unfinished workspace operations

Revision ID: 591de3f10346
Revises: 86fd2767ba46
Create Date: 2026-10-30 14:13:39.070063
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '591de3f10346'
down_revision: Union[str, Sequence[str], None] = '86fd2767ba46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The worker claims an operation as `running` before calling its provider, so a
    # restart must find claimed-but-unfinished operations as well as pending ones.
    op.drop_index("ix_operations_pending", table_name="operations")
    op.create_index("ix_operations_active", "operations", ["key"],
                    sqlite_where=sa.text("json_extract(body, '$.snapshot.status') IN ('pending', 'running')"))


def downgrade() -> None:
    op.drop_index("ix_operations_active", table_name="operations")
    op.create_index("ix_operations_pending", "operations", ["key"],
                    sqlite_where=sa.text("json_extract(body, '$.snapshot.status') = 'pending'"))
