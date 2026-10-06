"""add graph registry, run submissions, loops, node executions and steering

Revision ID: 2fa9e9c0a265
Revises: d3f81a6c2e90
Create Date: 2026-10-05 09:41:12.503118
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2fa9e9c0a265'
down_revision: Union[str, Sequence[str], None] = 'd3f81a6c2e90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """The records `engine-graph-service` keeps beside the graph runtime's own.

    In the graph database rather than a file of their own because every one of
    them is about runs this database already holds: a registered graph version
    is the id a run was started under, a submission names the run it created,
    and a loop's spend and pull requests are read off `events` and
    `github_pull_requests` rather than copied.

    A graph is a name within a project; its versions are immutable, and a run
    pins one. Spend and pull request counts are deliberately not columns on
    `cli_loops`: they are recomputed from the durable events, so a restart can
    neither forget nor double-count them.
    """
    op.create_table(
        "cli_graphs",
        sa.Column("graph_id", sa.Text(), primary_key=True),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("latest_version_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("project", "name", name="cli_graphs_by_name"),
    )
    op.create_table(
        "cli_graph_versions",
        sa.Column("version_id", sa.Text(), primary_key=True),
        sa.Column("graph_id", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("graph_id", "number", name="cli_graph_versions_by_number"),
    )
    op.create_table(
        "cli_run_submissions",
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("cli_run_submissions_by_run", "cli_run_submissions", ["run_id"])
    op.create_table(
        "cli_loops",
        sa.Column("loop_id", sa.Text(), primary_key=True),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("graph_id", sa.Text(), nullable=False),
        sa.Column("version_id", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("repository", sa.Text(), nullable=False),
        sa.Column("inputs", sa.Text(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("max_prs", sa.Integer()),
        sa.Column("max_spend_usd", sa.Float()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("pause_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_run_at", sa.Text(), nullable=False),
        sa.Column("active_run_id", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("project", "name", name="cli_loops_by_name"),
    )
    op.create_table(
        "cli_loop_runs",
        sa.Column("loop_id", sa.Text(), nullable=False),
        sa.Column("tick", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        # One run per scheduled tick, so a duplicate tick cannot start a second.
        sa.PrimaryKeyConstraint("loop_id", "tick"),
    )
    op.create_index("cli_loop_runs_by_run", "cli_loop_runs", ["run_id"])
    op.create_table(
        "cli_node_executions",
        sa.Column("execution_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("cli_node_executions_by_run", "cli_node_executions", ["run_id"])
    op.create_table(
        "cli_steering",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("steering_id", sa.Text(), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("execution_id", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("accepted_at", sa.Text(), nullable=False),
        sa.Column("delivered_at", sa.Text()),
        sa.Column("applied_at", sa.Text()),
        sqlite_autoincrement=True,
    )
    op.create_index("cli_steering_by_execution", "cli_steering", ["execution_id"])


def downgrade() -> None:
    op.drop_index("cli_steering_by_execution", table_name="cli_steering")
    op.drop_table("cli_steering")
    op.drop_index("cli_node_executions_by_run", table_name="cli_node_executions")
    op.drop_table("cli_node_executions")
    op.drop_index("cli_loop_runs_by_run", table_name="cli_loop_runs")
    op.drop_table("cli_loop_runs")
    op.drop_table("cli_loops")
    op.drop_index("cli_run_submissions_by_run", table_name="cli_run_submissions")
    op.drop_table("cli_run_submissions")
    op.drop_table("cli_graph_versions")
    op.drop_table("cli_graphs")
