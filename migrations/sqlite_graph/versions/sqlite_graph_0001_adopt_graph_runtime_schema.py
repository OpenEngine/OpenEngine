"""Adopt graph runtime schema

Revision ID: sqlite_graph_0001
Revises:
Create Date: 2026-09-09 10:03:12.923961
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'sqlite_graph_0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Adopt databases created by the former startup DDL without losing records.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "events" not in tables:
        op.create_table(
            "events",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("node_id", sa.Text()),
            sa.Column("execution_id", sa.Text()),
            sqlite_autoincrement=True,
        )
    if "runs" not in tables:
        op.create_table(
            "runs",
            sa.Column("run_id", sa.Text(), primary_key=True),
            sa.Column("graph_id", sa.Text(), nullable=False),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("ordinal", sa.Integer()),
        )
    if "sessions" not in tables:
        op.create_table(
            "sessions",
            sa.Column("run_id", sa.Text(), primary_key=True),
            sa.Column("session_key", sa.Text(), primary_key=True),
            sa.Column("continuation", sa.Text(), nullable=False),
        )
    if "approvals" not in tables:
        op.create_table(
            "approvals",
            sa.Column("approval_id", sa.Text(), primary_key=True),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("record", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("decision", sa.Text()),
            sa.Column("ordinal", sa.Integer()),
        )
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("runs")}
    if "auto_approve_nodes" not in columns:
        op.add_column(
            "runs",
            sa.Column("auto_approve_nodes", sa.Text(), nullable=False, server_default="[]"),
        )
    for table, name, columns in (
        ("events", "events_by_run", ["run_id", "sequence"]),
        ("approvals", "approvals_by_run", ["run_id"]),
    ):
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
        if name not in indexes:
            op.create_index(name, table, columns)


def downgrade() -> None:
    for table in ("approvals", "sessions", "runs", "events"):
        op.drop_table(table)
