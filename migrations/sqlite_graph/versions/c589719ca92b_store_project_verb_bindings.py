"""store project verb bindings

Revision ID: c589719ca92b
Revises: 17c1558d2a6b
Create Date: 2026-10-12 16:21:07.296062
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c589719ca92b'
down_revision: Union[str, Sequence[str], None] = '17c1558d2a6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("cli_projects", sa.Column("name", sa.Text(), primary_key=True),
                    sa.Column("verbs", sa.Text(), nullable=False))


def downgrade() -> None:
    op.drop_table("cli_projects")
