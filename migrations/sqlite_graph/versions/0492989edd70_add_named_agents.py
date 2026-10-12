"""add named agents

Revision ID: 0492989edd70
Revises: 1395ea8ffaf6
Create Date: 2026-10-08 11:55:38.856094
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0492989edd70'
down_revision: Union[str, Sequence[str], None] = '1395ea8ffaf6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Agents added with `engine agent add`, beside the built-in claude, codex and opencode.

    A graph names its agent, so the name is the key. `kind` is which harness
    runs it; `model` is what it runs when a node names none; `url` is an
    OpenAI-compatible endpoint, for an opencode agent on a model server.
    """
    op.create_table(
        "cli_agents",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cli_agents")
