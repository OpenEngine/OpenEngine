"""Adopt the existing SQLite schema into Alembic.

Revision ID: sqlite_0001
Revises:
"""

from collections.abc import Callable

from alembic import op
import sqlalchemy as sa


revision = "sqlite_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())

    _create_if_missing(
        existing_tables,
        "agent_instances",
        lambda: op.create_table(
            "agent_instances",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("instance_id", sa.Text(), nullable=False, unique=True),
            sa.Column("agent_id", sa.Text(), nullable=False),
            sa.Column("conversation_id", sa.Text(), nullable=False, unique=True),
            sa.Column("task_id", sa.Text()),
            sa.Column("workspace_id", sa.Text()),
            sa.Column(
                "title", sa.Text(), nullable=False, server_default="New chat"
            ),
            sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("runner", sa.Text(), nullable=False, server_default=""),
            sa.Column("auto_approve", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("workflow_run_id", sa.Text()),
            sa.Column("workflow_step_id", sa.Text()),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "projects",
        lambda: op.create_table(
            "projects",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.Text(), nullable=False, unique=True),
            sa.Column("name", sa.Text(), nullable=False),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "milestones",
        lambda: op.create_table(
            "milestones",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("milestone_id", sa.Text(), nullable=False, unique=True),
            sa.Column("project_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"]),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "workstreams",
        lambda: op.create_table(
            "workstreams",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("workstream_id", sa.Text(), nullable=False, unique=True),
            sa.Column("milestone_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["milestone_id"], ["milestones.milestone_id"]),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "run_states",
        lambda: op.create_table(
            "run_states",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Text(), nullable=False, unique=True),
            sa.Column("state_json", sa.Text(), nullable=False),
            sa.Column("workstream_id", sa.Text()),
            sa.ForeignKeyConstraint(["workstream_id"], ["workstreams.workstream_id"]),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "run_events",
        lambda: op.create_table(
            "run_events",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("event_json", sa.Text(), nullable=False),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "messages",
        lambda: op.create_table(
            "messages",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("instance_id", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("tool_calls", sa.Text(), nullable=False),
            sa.Column("tool_call_id", sa.Text()),
            sa.ForeignKeyConstraint(["instance_id"], ["agent_instances.instance_id"]),
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "agent_runs",
        lambda: op.create_table(
            "agent_runs",
            sa.Column("agent_run_id", sa.Text(), primary_key=True),
            sa.Column("instance_id", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("changed_files", sa.Text(), nullable=False),
            sa.Column("runner", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["instance_id"], ["agent_instances.instance_id"]),
        ),
    )
    _create_if_missing(
        existing_tables,
        "approvals",
        lambda: op.create_table(
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
            sqlite_autoincrement=True,
        ),
    )
    _create_if_missing(
        existing_tables,
        "session_grants",
        lambda: op.create_table(
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
            sqlite_autoincrement=True,
        ),
    )

    _add_missing_columns("agent_instances", _agent_instance_columns())
    run_state_columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("run_states")
    }
    if "workstream_id" not in run_state_columns:
        # SQLite can add a nullable REFERENCES column directly, but Alembic's
        # generic add_column operation tries to add its constraint separately.
        op.execute(
            "ALTER TABLE run_states ADD COLUMN workstream_id TEXT "
            "REFERENCES workstreams(workstream_id)"
        )
    _add_missing_columns(
        "approvals",
        [
            ("workspace_id", sa.Column("workspace_id", sa.Text())),
            ("tool_call_id", sa.Column("tool_call_id", sa.Text())),
            ("questions", sa.Column("questions", sa.Text())),
            ("answers", sa.Column("answers", sa.Text())),
        ],
    )

    _create_index_if_missing("milestones", "milestones_by_project", ["project_id"])
    _create_index_if_missing(
        "workstreams", "workstreams_by_milestone", ["milestone_id"]
    )
    _create_index_if_missing("run_states", "runs_by_workstream", ["workstream_id"])
    _create_index_if_missing("approvals", "approvals_by_run", ["agent_run_id"])
    _create_index_if_missing(
        "session_grants", "session_grants_by_instance", ["instance_id"]
    )


def downgrade() -> None:
    for table in (
        "session_grants",
        "approvals",
        "agent_runs",
        "messages",
        "run_events",
        "run_states",
        "workstreams",
        "milestones",
        "projects",
        "agent_instances",
    ):
        op.drop_table(table)


def _create_if_missing(
    existing_tables: set[str], table: str, create: Callable[[], object]
) -> None:
    if table not in existing_tables:
        create()


def _agent_instance_columns() -> list[tuple[str, sa.Column]]:
    return [
        (
            "title",
            sa.Column("title", sa.Text(), nullable=False, server_default="New chat"),
        ),
        (
            "archived",
            sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
        ),
        (
            "runner",
            sa.Column("runner", sa.Text(), nullable=False, server_default=""),
        ),
        (
            "auto_approve",
            sa.Column("auto_approve", sa.Integer(), nullable=False, server_default="0"),
        ),
        ("workflow_run_id", sa.Column("workflow_run_id", sa.Text())),
        ("workflow_step_id", sa.Column("workflow_step_id", sa.Text())),
    ]


def _add_missing_columns(
    table: str, columns: list[tuple[str, sa.Column]]
) -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }
    for name, column in columns:
        if name not in existing:
            op.add_column(table, column)


def _create_index_if_missing(table: str, name: str, columns: list[str]) -> None:
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)
