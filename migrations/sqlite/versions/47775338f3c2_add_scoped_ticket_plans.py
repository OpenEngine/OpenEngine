"""add scoped ticket plans

Revision ID: 47775338f3c2
Revises: d487944fbd15
Create Date: 2026-10-05 15:03:23.039841
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '47775338f3c2'
down_revision: Union[str, Sequence[str], None] = 'd487944fbd15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scoping_plans",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Text(), nullable=False, unique=True),
        sa.Column("loop_id", sa.Text(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("scoping_plans_by_loop", "scoping_plans", ["loop_id"])


def downgrade() -> None:
    op.drop_index("scoping_plans_by_loop", table_name="scoping_plans")
    op.drop_table("scoping_plans")
