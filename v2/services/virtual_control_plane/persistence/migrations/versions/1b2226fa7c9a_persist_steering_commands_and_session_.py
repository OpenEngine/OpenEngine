"""Persist steering commands and session event history

Revision ID: 1b2226fa7c9a
Revises: cfed9cf520fd
Create Date: 2026-11-03 04:24:33.848765
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1b2226fa7c9a'
down_revision: Union[str, Sequence[str], None] = 'cfed9cf520fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runtime_commands") as batch:
        batch.drop_constraint("ck_runtime_commands_status", type_="check")
        batch.create_check_constraint("ck_runtime_commands_status",
                                      "status IN ('pending', 'acknowledged', 'rejected', 'blocked', 'settled')")
    for name in ("steering_commands", "history_cursors"):
        op.create_table(name, sa.Column("key", sa.String(128), primary_key=True),
                        sa.Column("body", sa.JSON(), nullable=False))
    op.create_index("ix_steering_commands_pending", "steering_commands",
                    [sa.text("json_extract(body, '$.target.attempt_id')"), "key"],
                    sqlite_where=sa.text("json_extract(body, '$.status') = 'pending'"))
    op.create_table(
        "session_history",
        sa.Column("session_id", sa.String(128), sa.ForeignKey("sessions.key"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.key"), nullable=False),
        sa.Column("entry", sa.JSON(), nullable=False))
    for name, columns in (
        ("ix_session_history_kind", ["session_id", "kind", "sequence"]),
        ("ix_session_history_attempt", ["session_id", "attempt_id", "sequence"]),
        ("ix_session_history_attempt_kind", ["session_id", "attempt_id", "kind", "sequence"]),
    ):
        op.create_index(name, "session_history", columns)

    # Conversation progress predates the query. Preserve its original sequence,
    # including envelopes already published, rather than starting history anew.
    connection = op.get_bind()
    envelopes = connection.execute(sa.text(
        "SELECT envelope FROM outbox WHERE json_extract(envelope, '$.event_type') = 'conversation.message'"
    ).columns(envelope=sa.JSON())).scalars()
    history = sa.table("session_history", sa.column("session_id"), sa.column("sequence"),
                       sa.column("kind"), sa.column("attempt_id"), sa.column("entry", sa.JSON()))
    for envelope in envelopes:
        resources, payload = envelope["resources"], envelope["payload"]
        entry = {"sequence": payload["sequence"], "occurred_at": envelope["occurred_at"], "kind": "message",
                 "attempt_id": resources["attempt_id"], "message_id": resources["message_id"],
                 "role": payload["role"], "text": payload["text"]}
        connection.execute(history.insert().values(session_id=resources["session_id"], sequence=payload["sequence"],
                                                   kind="message", attempt_id=resources["attempt_id"], entry=entry))

    # Older documents lack history_sequence. Preserve counters already ahead.
    connection.execute(sa.text("""
        UPDATE sessions SET body = json_set(body, '$.history_sequence', max(
            coalesce(json_extract(body, '$.history_sequence'), 0),
            (SELECT max(sequence) FROM session_history WHERE session_id = sessions.key)))
        WHERE EXISTS (SELECT 1 FROM session_history WHERE session_id = sessions.key)
    """))


def downgrade() -> None:
    # Settled steering is no longer deliverable; preserve that on rollback.
    op.execute("UPDATE runtime_commands SET status = 'rejected' WHERE status = 'settled'")
    with op.batch_alter_table("runtime_commands") as batch:
        batch.drop_constraint("ck_runtime_commands_status", type_="check")
        batch.create_check_constraint("ck_runtime_commands_status",
                                      "status IN ('pending', 'acknowledged', 'rejected', 'blocked')")
    op.drop_table("session_history")
    op.drop_table("history_cursors")
    op.drop_table("steering_commands")
