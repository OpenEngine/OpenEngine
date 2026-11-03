"""The daemon's runtime-local journal: one SQLite file per runtime generation.

Alembic (`migrations/`) owns the schema; opening the store never creates it.
This is not a tenant service database: it lives with the runtime, holds one
workspace's binding, and is never discovered as a VCP-side service store.
Updates keep their exact encoded bytes after acknowledgement, for replay.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from openengine.contracts.runtime import error
from openengine.persistence import migrations
from openengine.persistence.sqlite import SqliteDatabase

MIGRATIONS = Path(__file__).parent / "migrations"
NAME = "workspace-daemon"

TERMINAL = ("completed", "failed", "cancelled")
STATES = ("recorded", "launching", "running", "cancelling", *TERMINAL)
_ACTIVE = "state NOT IN ('completed', 'failed', 'cancelled')"


def _choices(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=f"ck_{table}_{column}")


# Normalized: each fact is stored once and related by foreign key. Protocol
# messages keep their exact encoded bytes (`command`, `wire`) as the evidence
# they are; every field the daemon queries or updates is its own column.
metadata = sa.MetaData()
runtime = sa.Table(
    "journal_runtime", metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
    sa.CheckConstraint("id = 1", name="ck_journal_runtime_single"),
    sa.Column("workspace_id", sa.String(128), nullable=False),
    sa.Column("runtime_id", sa.String(128), nullable=False),
    sa.Column("runtime_generation", sa.Integer(), nullable=False),
    sa.Column("journal_id", sa.String(128), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("ready", sa.Boolean(), nullable=False),
    sa.Column("ready_sequence", sa.Integer(), sa.ForeignKey("journal_updates.sequence"), nullable=True),
    sa.Column("blocked_code", sa.String(64), nullable=True),
    sa.Column("blocked_sequence", sa.Integer(), sa.ForeignKey("journal_updates.sequence"), nullable=True),
    sa.Column("blocked_event_id", sa.String(128), nullable=True),
    sa.CheckConstraint("(blocked_code IS NULL) = (blocked_sequence IS NULL)", name="ck_journal_runtime_blocked"),
    comment="The single provisioned binding, journal identity, update high-water mark and readiness.",
)
commands = sa.Table(
    "journal_commands", metadata,
    sa.Column("command_id", sa.String(128), primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("command", sa.LargeBinary(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    _choices("journal_commands", "status", ("accepted", "rejected")),
    sa.Column("error_code", sa.String(64), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.CheckConstraint("(status = 'accepted') = (error_code IS NULL)", name="ck_journal_commands_error"),
    comment="Remembered execute and cancel commands as received and their final reply.",
)
sessions = sa.Table(
    "journal_sessions", metadata,
    sa.Column("session_id", sa.String(128), primary_key=True),
    sa.Column("command_id", sa.String(128), sa.ForeignKey("journal_commands.command_id"), nullable=False),
    # The executor's own conversation (an ACP session ID) and whether it can be loaded again.
    sa.Column("executor_session_id", sa.String(1024), nullable=True),
    sa.Column("executor_resumable", sa.Boolean(), nullable=False, server_default=sa.false()),
    comment="Sessions owned by this runtime; the opening command carries their agent.",
)
attempts = sa.Table(
    "journal_attempts", metadata,
    sa.Column("attempt_id", sa.String(128), primary_key=True),
    # NULL only for a cancel-before-execute tombstone, which never opens its session.
    sa.Column("session_id", sa.String(128), sa.ForeignKey("journal_sessions.session_id"), nullable=True),
    # The execute command, or for a tombstone the cancel command that created it.
    sa.Column("command_id", sa.String(128), sa.ForeignKey("journal_commands.command_id"), nullable=False, unique=True),
    # The first (winning) cancel journaled for the attempt; it decides a later terminal record.
    sa.Column("cancel_command_id", sa.String(128), sa.ForeignKey(
        "journal_commands.command_id", name="fk_journal_attempts_cancel_command_id_journal_commands"), nullable=True),
    sa.Column("state", sa.String(16), nullable=False),
    _choices("journal_attempts", "state", STATES),
    sa.Column("launch_count", sa.Integer(), nullable=False),
    sa.Column("attempt_sequence", sa.Integer(), nullable=False),
    sa.Column("last_event_sequence", sa.Integer(), nullable=False),
    # Opaque to the daemon: owned and interpreted only by the executor port.
    sa.Column("executor_handle", sa.JSON(), nullable=True),
    # At most one unfinished attempt per session; active ones are counted for capacity.
    sa.Index("ux_journal_attempts_active_session", "session_id", unique=True, sqlite_where=sa.text(_ACTIVE)),
    sa.Index("ix_journal_attempts_state", "state", sqlite_where=sa.text(_ACTIVE)),
    comment="Accepted attempts and their execution progress.",
)
updates = sa.Table(
    "journal_updates", metadata,
    sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
    sa.Column("event_id", sa.String(128), nullable=False),
    sa.Column("attempt_id", sa.String(128), sa.ForeignKey("journal_attempts.attempt_id"), nullable=True),
    sa.Column("wire", sa.LargeBinary(), nullable=False),
    sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Index("ix_journal_updates_event_id", "event_id", unique=True),
    sa.Index("ix_journal_updates_pending", "sequence", sqlite_where=sa.text("acknowledged = 0")),
    comment="Ordered runtime updates as sent, retained after VCP commits them.",
)


def database(directory: Path) -> SqliteDatabase:
    return SqliteDatabase(NAME, directory)


def upgrade(journal: SqliteDatabase) -> migrations.Upgrade:
    return migrations.upgrade(MIGRATIONS, journal)


def _decode(wire: bytes) -> dict[str, Any]:
    return json.loads(wire)


def _encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


_CANCEL = commands.alias("cancel_commands")
_ATTEMPT = (sa.select(attempts, commands.c.command, _CANCEL.c.command.label("cancel"))
            .join(commands, attempts.c.command_id == commands.c.command_id)
            .outerjoin(_CANCEL, attempts.c.cancel_command_id == _CANCEL.c.command_id))


def _attempt(row: Any) -> dict[str, Any]:
    return {"command": _decode(row.command), "state": row.state, "launch_count": row.launch_count,
            "attempt_sequence": row.attempt_sequence, "last_event_sequence": row.last_event_sequence,
            "executor_handle": row.executor_handle, "cancel": row.cancel and _decode(row.cancel)}


class Transaction:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def _first(self, query: Any) -> Any:
        return (await self._connection.execute(query)).first()

    async def runtime(self) -> dict[str, Any] | None:
        row = await self._first(sa.select(runtime))
        if row is None:
            return None
        blocked = None if row.blocked_code is None else {
            "code": row.blocked_code, "sequence": row.blocked_sequence, "event_id": row.blocked_event_id}
        return {"target": {"workspace_id": row.workspace_id, "runtime_id": row.runtime_id,
                           "runtime_generation": row.runtime_generation},
                "journal_id": row.journal_id, "sequence": row.sequence, "ready": row.ready,
                "ready_sequence": row.ready_sequence, "blocked": blocked}

    async def save_runtime(self, meta: dict[str, Any]) -> None:
        blocked = meta.get("blocked") or {}
        values = {**meta["target"], "journal_id": meta["journal_id"], "sequence": meta["sequence"],
                  "ready": bool(meta.get("ready")), "ready_sequence": meta.get("ready_sequence"),
                  "blocked_code": blocked.get("code"), "blocked_sequence": blocked.get("sequence"),
                  "blocked_event_id": blocked.get("event_id")}
        statement = insert(runtime).values(id=1, **values)
        await self._connection.execute(statement.on_conflict_do_update(index_elements=["id"], set_=values))

    async def active_attempts(self) -> int:
        query = sa.select(sa.func.count()).select_from(attempts).where(sa.text(_ACTIVE))
        return (await self._connection.execute(query)).scalar_one()

    async def receipt(self, command_id: str) -> dict[str, Any] | None:
        """A remembered command's digest and its original reply, rebuilt from columns."""
        row = await self._first(sa.select(commands).where(commands.c.command_id == command_id))
        if row is None:
            return None
        response = {"command_id": command_id, "target": _decode(row.command)["target"], "status": row.status}
        if row.error_code is not None:
            response["error"] = error(row.error_code, row.error_message)
        return {"digest": row.digest, "response": response}

    async def remember(self, command: dict[str, Any], digest: str, response: dict[str, Any]) -> None:
        detail = response.get("error") or {}
        await self._connection.execute(commands.insert().values(
            command_id=command["command_id"], digest=digest, command=_encode(command), status=response["status"],
            error_code=detail.get("code"), error_message=detail.get("message")))

    async def session(self, session_id: str) -> dict[str, Any] | None:
        """The session's agent, from its opening command, its executor conversation and its unfinished attempt."""
        query = (sa.select(commands.c.command, sessions.c.executor_session_id, sessions.c.executor_resumable)
                 .join(sessions, sessions.c.command_id == commands.c.command_id)
                 .where(sessions.c.session_id == session_id))
        row = await self._first(query)
        if row is None:
            return None
        active = await self._first(sa.select(attempts.c.attempt_id)
                                   .where(attempts.c.session_id == session_id, sa.text(_ACTIVE)))
        return {"agent": _decode(row.command)["payload"]["agent"], "active_attempt": active and active.attempt_id,
                "executor_session_id": row.executor_session_id, "executor_resumable": row.executor_resumable}

    async def open_session(self, session_id: str, command_id: str) -> None:
        await self._connection.execute(sessions.insert().values(session_id=session_id, command_id=command_id))

    async def save_executor_session(self, session_id: str, executor_session_id: str, resumable: bool) -> None:
        await self._connection.execute(sessions.update().where(sessions.c.session_id == session_id).values(
            executor_session_id=executor_session_id, executor_resumable=resumable))

    async def attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = await self._first(_ATTEMPT.where(attempts.c.attempt_id == attempt_id))
        return None if row is None else _attempt(row)

    async def attempts(self, state: str | None = None) -> list[dict[str, Any]]:
        query = _ATTEMPT if state is None else _ATTEMPT.where(attempts.c.state == state)
        return [_attempt(row) for row in await self._connection.execute(query.order_by(attempts.c.attempt_id))]

    async def attempt_ids(self, *states: str) -> list[str]:
        query = sa.select(attempts.c.attempt_id).where(attempts.c.state.in_(states)).order_by(attempts.c.attempt_id)
        return list((await self._connection.execute(query)).scalars())

    async def save_attempt(self, attempt: dict[str, Any]) -> None:
        command, cancel = attempt["command"], attempt.get("cancel")
        values = {key: attempt.get(key) for key in (
            "state", "launch_count", "attempt_sequence", "last_event_sequence", "executor_handle")}
        values["cancel_command_id"] = cancel and cancel["command_id"]
        session_id = None if command["kind"] == "cancel" else command["target"]["session_id"]
        statement = insert(attempts).values(attempt_id=command["target"]["attempt_id"], command_id=command["command_id"],
                                            session_id=session_id, **values)
        await self._connection.execute(statement.on_conflict_do_update(index_elements=["attempt_id"], set_=values))

    async def append_update(self, sequence: int, event_id: str, wire: bytes, attempt_id: str | None = None) -> None:
        await self._connection.execute(updates.insert().values(
            sequence=sequence, event_id=event_id, attempt_id=attempt_id, wire=wire))

    async def oldest_pending(self) -> tuple[int, bytes] | None:
        query = (sa.select(updates.c.sequence, updates.c.wire).where(updates.c.acknowledged.is_(False))
                 .order_by(updates.c.sequence).limit(1))
        row = (await self._connection.execute(query)).first()
        return None if row is None else (row.sequence, row.wire)

    async def pending_updates(self) -> int:
        """Unacknowledged updates, counted through the pending partial index."""
        query = sa.select(sa.func.count()).select_from(updates).where(updates.c.acknowledged.is_(False))
        return (await self._connection.execute(query)).scalar_one()

    async def has_update(self, sequence: int) -> bool:
        query = sa.select(updates.c.sequence).where(updates.c.sequence == sequence)
        return (await self._connection.execute(query)).first() is not None

    async def acknowledge(self, sequence: int) -> None:
        await self._connection.execute(updates.update().where(updates.c.sequence == sequence).values(acknowledged=True))

    async def replay_from(self, sequence: int) -> None:
        """Resend retained updates from `sequence`, unchanged, before anything later."""
        await self._connection.execute(updates.update().where(updates.c.sequence >= sequence).values(acknowledged=False))


class JournalStore:
    def __init__(self, journal: SqliteDatabase) -> None:
        self.database = journal

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        async with self.database.connect() as connection:
            try:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                yield Transaction(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Transaction]:
        """A read-only view that never reserves the writer, so inspect stays responsive."""
        async with self.database.connect() as connection:
            yield Transaction(connection)

    async def close(self) -> None:
        await self.database.close()
