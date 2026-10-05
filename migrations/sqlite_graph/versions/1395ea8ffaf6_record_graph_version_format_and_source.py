"""record graph version format and source

Revision ID: 1395ea8ffaf6
Revises: 2fa9e9c0a265
Create Date: 2026-10-05 11:57:48.115146
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1395ea8ffaf6'
down_revision: Union[str, Sequence[str], None] = '2fa9e9c0a265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Keep each graph version's source verbatim, and say which language it is.

    A graph is a YAML file or a Python LangGraph file. The canonical
    `manifest` was enough while there was one language; with two, the source
    is what a version is, and what `engine graph get` gives back. Versions
    registered before this were YAML-shaped manifests, so they read as `yaml`
    with no source of their own.
    """
    with op.batch_alter_table("cli_graph_versions") as batch:
        batch.add_column(sa.Column("format", sa.Text(), nullable=False, server_default="yaml"))
        batch.add_column(sa.Column("source", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("cli_graph_versions") as batch:
        batch.drop_column("source")
        batch.drop_column("format")
