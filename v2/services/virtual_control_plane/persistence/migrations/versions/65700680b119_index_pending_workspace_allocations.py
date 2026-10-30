"""index pending workspace allocations

Revision ID: 65700680b119
Revises: c36db826eebb
Create Date: 2026-10-30 12:07:56.678555
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '65700680b119'
down_revision: Union[str, Sequence[str], None] = 'c36db826eebb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_operations_pending", "operations", ["key"],
                    sqlite_where=sa.text("json_extract(body, '$.snapshot.status') = 'pending'"))


def downgrade() -> None:
    op.drop_index("ix_operations_pending", table_name="operations")
