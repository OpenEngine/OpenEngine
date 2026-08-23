"""Create the initial PostgreSQL schema.

Revision ID: postgres_0001
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "postgres_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_instances",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.Text(), nullable=False, unique=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Text(), nullable=False, unique=True),
        sa.Column("task_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False, server_default="New chat"),
        sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runner", sa.Text(), nullable=False, server_default=""),
        sa.Column("auto_approve", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("workflow_run_id", sa.Text()),
        sa.Column("workflow_step_id", sa.Text()),
    )
    op.create_table(
        "projects",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
    )
    op.create_table(
        "milestones",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("milestone_id", sa.Text(), nullable=False, unique=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"]),
    )
    op.create_index("milestones_by_project", "milestones", ["project_id"])
    op.create_table(
        "workstreams",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workstream_id", sa.Text(), nullable=False, unique=True),
        sa.Column("milestone_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.milestone_id"]),
    )
    op.create_index("workstreams_by_milestone", "workstreams", ["milestone_id"])
    op.create_table(
        "run_states",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False, unique=True),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("workstream_id", sa.Text()),
        sa.ForeignKeyConstraint(["workstream_id"], ["workstreams.workstream_id"]),
    )
    op.create_index("runs_by_workstream", "run_states", ["workstream_id"])
    op.create_table(
        "run_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("event_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text()),
        sa.ForeignKeyConstraint(["instance_id"], ["agent_instances.instance_id"]),
    )
    op.create_table(
        "agent_runs",
        sa.Column("agent_run_id", sa.Text(), primary_key=True),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("changed_files", sa.Text(), nullable=False),
        sa.Column("runner", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["agent_instances.instance_id"]),
    )
    op.create_table(
        "approvals",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("approval_id", sa.Text(), nullable=False, unique=True),
        sa.Column("agent_run_id", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("runner", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("command", sa.Text()),
        sa.Column("cwd", sa.Text()),
        sa.Column("tool_name", sa.Text()),
        sa.Column("tool_call_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("arguments", sa.Text()),
        sa.Column("questions", sa.Text()),
        sa.Column("answers", sa.Text()),
        sa.Column("allowed_decisions", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text()),
        sa.Column("decision_source", sa.Text()),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("decided_at", sa.Text()),
        sa.ForeignKeyConstraint(["instance_id"], ["agent_instances.instance_id"]),
    )
    op.create_index("approvals_by_run", "approvals", ["agent_run_id"])
    op.create_table(
        "session_grants",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("grant_id", sa.Text(), nullable=False, unique=True),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("runner", sa.Text(), nullable=False),
        sa.Column("approval_kind", sa.Text(), nullable=False),
        sa.Column("normalized_scope", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("created_from_approval_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.Text()),
        sa.ForeignKeyConstraint(["instance_id"], ["agent_instances.instance_id"]),
    )
    op.create_index("session_grants_by_instance", "session_grants", ["instance_id"])


def downgrade() -> None:
    op.drop_index("session_grants_by_instance", table_name="session_grants")
    op.drop_table("session_grants")
    op.drop_index("approvals_by_run", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("agent_runs")
    op.drop_table("messages")
    op.drop_table("run_events")
    op.drop_index("runs_by_workstream", table_name="run_states")
    op.drop_table("run_states")
    op.drop_index("workstreams_by_milestone", table_name="workstreams")
    op.drop_table("workstreams")
    op.drop_index("milestones_by_project", table_name="milestones")
    op.drop_table("milestones")
    op.drop_table("projects")
    op.drop_table("agent_instances")
