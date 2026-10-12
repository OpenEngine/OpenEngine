"""index graph events by run and kind

Revision ID: 90163fc5ff5b
Revises: 0492989edd70
Create Date: 2026-10-12 12:08:45.805506
"""

from typing import Sequence, Union

from alembic import op


revision: str = '90163fc5ff5b'
down_revision: Union[str, Sequence[str], None] = '0492989edd70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("events_by_run_kind", "events", ["run_id", "kind", "sequence"])


def downgrade() -> None:
    op.drop_index("events_by_run_kind", table_name="events")
