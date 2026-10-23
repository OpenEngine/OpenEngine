"""The tool service's tables. Migrations must keep the database in step with these.

Resources are keyed by `uid`, never by name: a Tool or ToolGrant deleted and
created again under the same name is a new row, so nothing granted to the old
one carries over.
"""

from __future__ import annotations

import sqlalchemy as sa

from openengine.persistence import UtcDateTime

metadata = sa.MetaData()

NAME = 63
"""Namespaces, names and uids are one NATS subject token of at most 63 characters."""

# An invocable capability bound to one connection, as last applied.
tools = sa.Table(
    "tools",
    metadata,
    sa.Column("uid", sa.String(NAME), primary_key=True),
    sa.Column("namespace", sa.String(NAME), nullable=False),
    sa.Column("name", sa.String(NAME), nullable=False),
    sa.Column("generation", sa.Integer(), nullable=False),
    sa.Column("labels", sa.JSON(), nullable=False),
    sa.Column("annotations", sa.JSON(), nullable=False),
    sa.Column("spec", sa.JSON(), nullable=False),
    # From spec.connectionRef, so a connection's change of state finds its tools.
    sa.Column("connection_name", sa.String(64), nullable=False),
    sa.Column("phase", sa.String(16), nullable=False),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("namespace", "name", name="uq_tools_namespace_name"),
    sa.Index("ix_tools_connection_name", "connection_name"),
)

# Who may call which tool. `tool_uid` is the tool the grant was bound to when
# applied; a grant whose tool has since been replaced denies every call.
tool_grants = sa.Table(
    "tool_grants",
    metadata,
    sa.Column("uid", sa.String(NAME), primary_key=True),
    sa.Column("namespace", sa.String(NAME), nullable=False),
    sa.Column("name", sa.String(NAME), nullable=False),
    sa.Column("generation", sa.Integer(), nullable=False),
    sa.Column("labels", sa.JSON(), nullable=False),
    sa.Column("annotations", sa.JSON(), nullable=False),
    sa.Column("tool_name", sa.String(NAME), nullable=False),
    sa.Column("tool_uid", sa.String(NAME), nullable=False),
    sa.Column("workspace_name", sa.String(NAME), nullable=False),
    # The Graph Engine's step attempt ID (`stepa_…`); null grants the whole workspace.
    sa.Column("step_attempt_id", sa.String(128), nullable=True),
    sa.Column("constraints", sa.JSON(), nullable=False),
    sa.Column("expires_at", UtcDateTime(), nullable=True),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.UniqueConstraint("namespace", "name", name="uq_tool_grants_namespace_name"),
    sa.Index("ix_tool_grants_subject", "namespace", "workspace_name", "tool_name"),
)

# One accepted call. The resolved tool definition is copied in, so changing or
# deleting the Tool cannot change accepted work; `grant_refs` names the grants
# (by uid) that allowed it, which are checked again before every dispatch.
tool_invocations = sa.Table(
    "tool_invocations",
    metadata,
    sa.Column("uid", sa.String(NAME), primary_key=True),
    sa.Column("namespace", sa.String(NAME), nullable=False),
    # The caller's invocationId.
    sa.Column("name", sa.String(NAME), nullable=False),
    sa.Column("tool_name", sa.String(NAME), nullable=False),
    sa.Column("tool_uid", sa.String(NAME), nullable=False),
    sa.Column("tool_generation", sa.Integer(), nullable=False),
    sa.Column("tool_spec", sa.JSON(), nullable=False),
    sa.Column("workspace_name", sa.String(NAME), nullable=False),
    sa.Column("workspace_generation", sa.Integer(), nullable=False),
    sa.Column("step_attempt_id", sa.String(128), nullable=False),
    sa.Column("arguments", sa.JSON(), nullable=False),
    sa.Column("grant_refs", sa.JSON(), nullable=False),
    sa.Column("phase", sa.String(16), nullable=False),
    sa.Column("attempt_count", sa.Integer(), nullable=False),
    sa.Column("result", sa.JSON(), nullable=True),
    sa.Column("error", sa.JSON(), nullable=True),
    sa.Column("created_at", UtcDateTime(), nullable=False),
    sa.Column("updated_at", UtcDateTime(), nullable=False),
    sa.Column("started_at", UtcDateTime(), nullable=True),
    sa.Column("completed_at", UtcDateTime(), nullable=True),
    sa.UniqueConstraint("namespace", "name", name="uq_tool_invocations_namespace_name"),
    sa.Index("ix_tool_invocations_caller", "namespace", "workspace_name", "step_attempt_id"),
    sa.Index("ix_tool_invocations_phase", "phase"),
)

# Each try at an invocation. `idempotency_key` is what a provider-idempotent
# tool repeats on every attempt; `outcome` stays null while an attempt runs or
# when an interrupted one could not be reconciled.
tool_invocation_attempts = sa.Table(
    "tool_invocation_attempts",
    metadata,
    sa.Column(
        "invocation_uid",
        sa.String(NAME),
        sa.ForeignKey("tool_invocations.uid", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("number", sa.Integer(), primary_key=True),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("outcome", sa.String(16), nullable=True),
    sa.Column("started_at", UtcDateTime(), nullable=False),
    sa.Column("finished_at", UtcDateTime(), nullable=True),
)
