"""add MCP OAuth grants

Revision ID: 1099c8b7900d
Revises: 473bfdc7cd0d
Create Date: 2026-09-24 12:07:56.130019
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1099c8b7900d'
down_revision: Union[str, Sequence[str], None] = '473bfdc7cd0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("oauth_clients",
        sa.Column("client_id", sa.Text(), primary_key=True),
        sa.Column("metadata", sa.Text(), nullable=False),
    )
    op.create_table("oauth_codes",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("expires", sa.Integer(), nullable=False),
    )
    op.create_table("oauth_refresh_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("family", sa.Text(), nullable=False, index=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("expires", sa.Integer(), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("oauth_refresh_tokens")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_clients")
