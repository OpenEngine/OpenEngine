"""store added agent context limits

Revision ID: 3293d6fa005f
Revises: a01559975f87
Create Date: 2026-10-15 13:38:56.096032
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3293d6fa005f'
down_revision: Union[str, Sequence[str], None] = 'a01559975f87'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """The context and output token limits an agent added with `--url` gives opencode.

    Without a context limit opencode never compacts, so prompts outgrow the
    window the model server was configured for. 0 leaves the limit unset.
    """
    with op.batch_alter_table("cli_agents") as batch:
        batch.add_column(sa.Column("context_limit", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("output_limit", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("cli_agents") as batch:
        batch.drop_column("output_limit")
        batch.drop_column("context_limit")
