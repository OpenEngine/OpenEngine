"""expire OAuth client registrations

Revision ID: a97e7badc246
Revises: 1099c8b7900d
Create Date: 2026-09-24 12:28:38.656823
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a97e7badc246'
down_revision: Union[str, Sequence[str], None] = '1099c8b7900d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oauth_clients", sa.Column("expires", sa.Integer(), nullable=False, server_default="0"))
    # Existing registrations receive one retention window from upgrade.
    op.execute("UPDATE oauth_clients SET expires = CAST(strftime('%s', 'now') AS INTEGER) + 2592000")


def downgrade() -> None:
    op.drop_column("oauth_clients", "expires")
