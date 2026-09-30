"""add autonomous projects

Revision ID: 9aa290353455
Revises: d487944fbd15
Create Date: 2026-09-30 09:31:10.667738
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9aa290353455'
down_revision: Union[str, Sequence[str], None] = 'd487944fbd15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "autonomous_projects",
        sa.Column("project_id", sa.Text(), primary_key=True),
        sa.Column("project_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("autonomous_projects")
