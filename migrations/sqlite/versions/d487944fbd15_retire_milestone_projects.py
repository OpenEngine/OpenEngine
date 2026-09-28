"""Retire milestone projects

Revision ID: d487944fbd15
Revises: 473bfdc7cd0d
Create Date: 2026-09-28 14:11:04.883136
"""

from alembic import op
import sqlalchemy as sa


revision = "d487944fbd15"
down_revision = "473bfdc7cd0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove the referencing column before its parent table; keep every run.
    with op.batch_alter_table("run_states") as batch:
        batch.drop_index("runs_by_milestone")
        batch.drop_constraint("runs_milestone_fk", type_="foreignkey")
        batch.drop_column("milestone_id")
    op.execute(
        "UPDATE run_states SET state_json = json_remove(state_json, '$.milestone_id') "
        "WHERE json_valid(state_json)"
    )
    op.drop_table("milestones")
    # Hidden legacy projects are deliberately discarded, not converted.
    op.execute("DELETE FROM projects")


def downgrade() -> None:
    # The retired plans cannot be recovered; restore only their schema.
    op.create_table(
        "milestones",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("milestone_id", sa.Text(), nullable=False, unique=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("dependencies", sa.Text(), nullable=False, server_default="[]"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"]),
        sqlite_autoincrement=True,
    )
    op.create_index("milestones_by_project", "milestones", ["project_id"])
    with op.batch_alter_table("run_states") as batch:
        batch.add_column(sa.Column("milestone_id", sa.Text()))
        batch.create_foreign_key(
            "runs_milestone_fk", "milestones", ["milestone_id"], ["milestone_id"]
        )
        batch.create_index("runs_by_milestone", ["milestone_id"])
