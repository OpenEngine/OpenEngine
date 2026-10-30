"""index workspace lookup and pages

Revision ID: 86fd2767ba46
Revises: 65700680b119
Create Date: 2026-10-30 13:04:07.249806
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '86fd2767ba46'
down_revision: Union[str, Sequence[str], None] = '65700680b119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_workspaces_name", "workspaces", [sa.text("json_extract(body, '$.snapshot.name')")])
    op.create_index("ix_workspaces_page", "workspaces", [sa.text("json_extract(body, '$.snapshot.created_at')"), sa.text('key')], sqlite_where=sa.text("json_type(body, '$.snapshot') = 'object'"))
    op.create_index("ix_workspaces_provider_page", "workspaces", [sa.text("json_extract(body, '$.snapshot.provider')"), sa.text("json_extract(body, '$.snapshot.created_at')"), sa.text('key')], sqlite_where=sa.text("json_type(body, '$.snapshot') = 'object'"))
    op.create_index("ix_workspaces_state_page", "workspaces", [sa.text("json_extract(body, '$.snapshot.state')"), sa.text("json_extract(body, '$.snapshot.created_at')"), sa.text('key')], sqlite_where=sa.text("json_type(body, '$.snapshot') = 'object'"))
    op.create_index("ix_workspaces_job_page", "workspaces", [sa.text("json_extract(body, '$.snapshot.labels.job_id')"), sa.text("json_extract(body, '$.snapshot.created_at')"), sa.text('key')], sqlite_where=sa.text("json_type(body, '$.snapshot') = 'object'"))


def downgrade() -> None:
    op.drop_index("ix_workspaces_name", table_name="workspaces")
    op.drop_index("ix_workspaces_page", table_name="workspaces")
    op.drop_index("ix_workspaces_provider_page", table_name="workspaces")
    op.drop_index("ix_workspaces_state_page", table_name="workspaces")
    op.drop_index("ix_workspaces_job_page", table_name="workspaces")
