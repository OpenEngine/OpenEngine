"""index session pages

Revision ID: 9b7f1ac760d9
Revises: 700f6cbb335e
Create Date: 2026-11-02 07:09:07.641471
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9b7f1ac760d9'
down_revision: Union[str, Sequence[str], None] = '700f6cbb335e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for name, fields in (("ix_sessions_workspace_page", ["workspace_id", "created_at"]),
                         ("ix_sessions_workspace_state_page", ["workspace_id", "state", "created_at"])):
        op.create_index(name, "sessions", [*[sa.text(f"json_extract(body, '$.snapshot.{field}')") for field in fields], sa.text("key")])


def downgrade() -> None:
    op.drop_index("ix_sessions_workspace_state_page", table_name="sessions")
    op.drop_index("ix_sessions_workspace_page", table_name="sessions")
