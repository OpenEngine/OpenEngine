"""Record who started each work order.

Revision ID: 473bfdc7cd0d
Revises: 36ad5285394c
Create Date: 2026-09-23 14:17:01.205780
"""

from alembic import op
import sqlalchemy as sa


revision = "473bfdc7cd0d"
down_revision = "36ad5285394c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Provider-qualified, e.g. `github:<id>:<login>` or `slack:<team>:<user>`.
    # Null when nobody identifiable asked, including every existing run.
    with op.batch_alter_table("run_states") as batch:
        batch.add_column(sa.Column("requester", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("run_states") as batch:
        batch.drop_column("requester")
