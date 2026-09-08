"""Add direct milestone relationships to workflow runs.

Revision ID: sqlite_0005
Revises: sqlite_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "sqlite_0005"
down_revision = "sqlite_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run_states") as batch:
        batch.add_column(sa.Column("milestone_id", sa.Text()))
        batch.create_foreign_key(
            "runs_milestone_fk", "milestones", ["milestone_id"], ["milestone_id"]
        )
        batch.create_index("runs_by_milestone", ["milestone_id"])


def downgrade() -> None:
    with op.batch_alter_table("run_states") as batch:
        batch.drop_index("runs_by_milestone")
        batch.drop_constraint("runs_milestone_fk", type_="foreignkey")
        batch.drop_column("milestone_id")
