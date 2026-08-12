"""State Store conversation persistence backed by SQLite.

This adapter implements the agent identity and conversation portion of
``engine.ports.StateStore`` using only Python's standard library. Run state and
event history are intentionally left for the durable workflow-store work.
"""

from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from engine.domain.agents import AgentInstance, AgentRun, AgentRunStatus
from engine.domain.chat import Conversation, Message, Role, ToolCall
from engine.domain.events import Event
from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ConversationId,
    MessageId,
    RunId,
    TaskId,
    WorkspaceId,
)
from engine.domain.state import RunState


class SQLiteStateStore:
    """Persist agent instances, runs, and conversations in one SQLite file.

    A single connection keeps ``:memory:`` useful in tests. SQLite calls are
    guarded because Streamlit may reuse a cached store from another thread.
    """

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_instances (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL UNIQUE,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL UNIQUE,
                    task_id TEXT,
                    workspace_id TEXT,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    archived INTEGER NOT NULL DEFAULT 0,
                    runner TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT NOT NULL,
                    tool_call_id TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    agent_run_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    changed_files TEXT NOT NULL,
                    runner TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(agent_instances)"
                )
            }
            if "title" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN title TEXT NOT NULL DEFAULT 'New chat'"
                )
            if "archived" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                )
            if "runner" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances "
                    "ADD COLUMN runner TEXT NOT NULL DEFAULT ''"
                )

    # Run state and event persistence are outside this conversation adapter's
    # scope, but the methods remain present so it has the StateStore shape.
    async def load(self, run_id: RunId) -> RunState | None:
        raise NotImplementedError("SQLite run-state persistence is not implemented")

    async def save(self, state: RunState) -> None:
        raise NotImplementedError("SQLite run-state persistence is not implemented")

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        raise NotImplementedError("SQLite event persistence is not implemented")

    async def history(self, run_id: RunId) -> Sequence[Event]:
        raise NotImplementedError("SQLite event persistence is not implemented")

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
        runner: str = "",
    ) -> AgentInstance:
        instance = AgentInstance(
            instance_id=AgentInstanceId(f"agi-{uuid4().hex[:12]}"),
            agent_id=agent_id,
            conversation_id=ConversationId(f"conv-{uuid4().hex[:12]}"),
            task_id=task_id,
            workspace_id=workspace_id,
            runner=runner,
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_instances (
                    instance_id, agent_id, conversation_id, task_id,
                    workspace_id, runner
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    instance.instance_id,
                    instance.agent_id,
                    instance.conversation_id,
                    instance.task_id,
                    instance.workspace_id,
                    instance.runner,
                ),
            )
        return instance

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
    ) -> AgentInstance:
        with self._lock, self._connection:
            updated = self._connection.execute(
                """
                UPDATE agent_instances
                SET title = ?, archived = ?, runner = ?
                WHERE instance_id = ?
                """,
                (title, archived, runner, instance_id),
            ).rowcount
            if not updated:
                raise KeyError(f"no agent instance {instance_id!r}")
        instance = await self.load_instance(instance_id)
        assert instance is not None
        return instance

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT instance_id, agent_id, conversation_id, task_id, workspace_id,
                       title, archived, runner
                FROM agent_instances WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
        return _instance_from_row(row) if row is not None else None

    async def attach_workspace(
        self, instance_id: AgentInstanceId, workspace_id: WorkspaceId | None
    ) -> AgentInstance:
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE agent_instances SET workspace_id = ? WHERE instance_id = ?",
                (workspace_id, instance_id),
            ).rowcount
            if not updated:
                raise KeyError(f"no agent instance {instance_id!r}")
        instance = await self.load_instance(instance_id)
        assert instance is not None  # just updated, under the same lock
        return instance

    async def list_instances(self, agent_id: AgentId | None = None) -> Sequence[AgentInstance]:
        query = """
            SELECT instance_id, agent_id, conversation_id, task_id, workspace_id,
                   title, archived, runner
            FROM agent_instances
        """
        parameters: tuple[str, ...] = ()
        if agent_id is not None:
            query += " WHERE agent_id = ?"
            parameters = (agent_id,)
        query += " ORDER BY sequence DESC"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(_instance_from_row(row) for row in rows)

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        with self._lock:
            instance = self._connection.execute(
                "SELECT conversation_id FROM agent_instances WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            if instance is None:
                return None
            rows = self._connection.execute(
                """
                SELECT sequence, role, content, tool_calls, tool_call_id
                FROM messages WHERE instance_id = ? ORDER BY sequence
                """,
                (instance_id,),
            ).fetchall()
        return Conversation(
            conversation_id=ConversationId(instance["conversation_id"]),
            instance_id=instance_id,
            messages=tuple(_message_from_row(row) for row in rows),
        )

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        with self._lock, self._connection:
            exists = self._connection.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ?", (instance_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"no agent instance {instance_id!r}")
            self._connection.executemany(
                """
                INSERT INTO messages (
                    instance_id, role, content, tool_calls, tool_call_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        instance_id,
                        message.role.value,
                        message.content,
                        json.dumps(
                            [
                                {
                                    "call_id": call.call_id,
                                    "name": call.name,
                                    "arguments": call.arguments,
                                }
                                for call in message.tool_calls
                            ]
                        ),
                        message.tool_call_id,
                    )
                    for message in messages
                ),
            )

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_runs (
                    agent_run_id, instance_id, status, summary, changed_files, runner
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_run_id) DO UPDATE SET
                    instance_id = excluded.instance_id,
                    status = excluded.status,
                    summary = excluded.summary,
                    changed_files = excluded.changed_files,
                    runner = excluded.runner
                """,
                (
                    agent_run.agent_run_id,
                    agent_run.instance_id,
                    agent_run.status.value,
                    agent_run.summary,
                    json.dumps(agent_run.changed_files),
                    agent_run.runner,
                ),
            )

    async def agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        """Read back one execution, matching the in-memory adapter's helper."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT agent_run_id, instance_id, status, summary, changed_files, runner
                FROM agent_runs WHERE agent_run_id = ?
                """,
                (agent_run_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentRun(
            agent_run_id=AgentRunId(row["agent_run_id"]),
            instance_id=AgentInstanceId(row["instance_id"]),
            status=AgentRunStatus(row["status"]),
            summary=row["summary"],
            changed_files=tuple(json.loads(row["changed_files"])),
            runner=row["runner"],
        )

    def close(self) -> None:
        """Release the underlying database connection."""
        with self._lock:
            self._connection.close()


def _instance_from_row(row: sqlite3.Row) -> AgentInstance:
    return AgentInstance(
        instance_id=AgentInstanceId(row["instance_id"]),
        agent_id=AgentId(row["agent_id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        task_id=TaskId(row["task_id"]) if row["task_id"] is not None else None,
        workspace_id=(
            WorkspaceId(row["workspace_id"]) if row["workspace_id"] is not None else None
        ),
        title=row["title"],
        archived=bool(row["archived"]),
        runner=row["runner"],
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    calls = tuple(ToolCall(**call) for call in json.loads(row["tool_calls"]))
    return Message(
        role=Role(row["role"]),
        content=row["content"],
        tool_calls=calls,
        tool_call_id=row["tool_call_id"],
        message_id=MessageId(f"msg-{row['sequence']:06d}"),
    )


__all__ = ["SQLiteStateStore"]
