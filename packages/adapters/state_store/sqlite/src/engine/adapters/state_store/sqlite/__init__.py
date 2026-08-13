"""Durable workflow-run and conversation persistence backed by SQLite."""

from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

from engine.domain.agents import AgentInstance, AgentRun, AgentRunStatus
from engine.domain.chat import Conversation, Message, Role, ToolCall
from engine.domain.events import (
    AgentRunCompleted,
    ChangesPublished,
    Event,
    HumanReviewCompleted,
    RunFailed,
    RunRequested,
    StepCompleted,
    WorkspaceProvisioned,
)
from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ConversationId,
    MessageId,
    RunId,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.domain.state import RunPhase, RunState
from engine.domain.workflow import StepOutput


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
                    runner TEXT NOT NULL DEFAULT '',
                    workflow_run_id TEXT,
                    workflow_step_id TEXT
                );

                CREATE TABLE IF NOT EXISTS run_states (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    state_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_json TEXT NOT NULL
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
            if "workflow_run_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances ADD COLUMN workflow_run_id TEXT"
                )
            if "workflow_step_id" not in columns:
                self._connection.execute(
                    "ALTER TABLE agent_instances ADD COLUMN workflow_step_id TEXT"
                )

    # --- workflow runs ----------------------------------------------------

    async def load(self, run_id: RunId) -> RunState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT state_json FROM run_states WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _state_from_dict(json.loads(row["state_json"])) if row else None

    async def save(self, state: RunState) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO run_states (run_id, state_json) VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET state_json = excluded.state_json
                """,
                (state.run_id, json.dumps(_state_to_dict(state))),
            )

    async def list_runs(self) -> Sequence[RunState]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT state_json FROM run_states ORDER BY sequence DESC"
            ).fetchall()
        return tuple(_state_from_dict(json.loads(row["state_json"])) for row in rows)

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        with self._lock, self._connection:
            self._connection.executemany(
                "INSERT INTO run_events (run_id, event_json) VALUES (?, ?)",
                ((run_id, json.dumps(_event_to_dict(event))) for event in events),
            )

    async def history(self, run_id: RunId) -> Sequence[Event]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_json FROM run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return tuple(_event_from_dict(json.loads(row["event_json"])) for row in rows)

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
        runner: str = "",
        *,
        instance_id: AgentInstanceId | None = None,
        conversation_id: ConversationId | None = None,
        workflow_run_id: RunId | None = None,
        workflow_step_id: StepId | None = None,
    ) -> AgentInstance:
        instance = AgentInstance(
            instance_id=instance_id or AgentInstanceId(f"agi-{uuid4().hex[:12]}"),
            agent_id=agent_id,
            conversation_id=conversation_id
            or ConversationId(f"conv-{uuid4().hex[:12]}"),
            task_id=task_id,
            workspace_id=workspace_id,
            runner=runner,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
        )
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ?",
                (instance.instance_id,),
            ).fetchone()
            if existing is not None:
                loaded = await self.load_instance(instance.instance_id)
                assert loaded is not None
                return loaded
            self._connection.execute(
                """
                INSERT INTO agent_instances (
                    instance_id, agent_id, conversation_id, task_id,
                    workspace_id, runner, workflow_run_id, workflow_step_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance.instance_id,
                    instance.agent_id,
                    instance.conversation_id,
                    instance.task_id,
                    instance.workspace_id,
                    instance.runner,
                    instance.workflow_run_id,
                    instance.workflow_step_id,
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
                       title, archived, runner, workflow_run_id, workflow_step_id
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

    async def list_instances(
        self,
        agent_id: AgentId | None = None,
        *,
        workflow_run_id: RunId | None = None,
    ) -> Sequence[AgentInstance]:
        query = """
            SELECT instance_id, agent_id, conversation_id, task_id, workspace_id,
                   title, archived, runner, workflow_run_id, workflow_step_id
            FROM agent_instances
        """
        filters: list[str] = []
        parameters: list[str] = []
        if agent_id is not None:
            filters.append("agent_id = ?")
            parameters.append(agent_id)
        if workflow_run_id is not None:
            filters.append("workflow_run_id = ?")
            parameters.append(workflow_run_id)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY sequence DESC"
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
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
        workflow_run_id=(
            RunId(row["workflow_run_id"])
            if row["workflow_run_id"] is not None
            else None
        ),
        workflow_step_id=(
            StepId(row["workflow_step_id"])
            if row["workflow_step_id"] is not None
            else None
        ),
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


def _output_to_dict(output: StepOutput) -> dict[str, str]:
    return {"name": output.name, "value": output.value}


def _step_to_dict(step: StepCompleted) -> dict[str, object]:
    return {
        "run_id": step.run_id,
        "step_id": step.step_id,
        "agent_run_id": step.agent_run_id,
        "outcome": step.outcome,
        "summary": step.summary,
        "outputs": [_output_to_dict(output) for output in step.outputs],
    }


def _step_from_dict(value: dict[str, object]) -> StepCompleted:
    return StepCompleted(
        run_id=RunId(str(value["run_id"])),
        step_id=StepId(str(value["step_id"])),
        agent_run_id=AgentRunId(str(value["agent_run_id"])),
        outcome=str(value["outcome"]),
        summary=str(value["summary"]),
        outputs=tuple(
            StepOutput(name=str(output["name"]), value=str(output["value"]))
            for output in value.get("outputs", [])
            if isinstance(output, dict)
        ),
    )


def _review_to_dict(review: HumanReviewCompleted) -> dict[str, object]:
    return {
        "run_id": review.run_id,
        "step_id": review.step_id,
        "approved": review.approved,
        "summary": review.summary,
    }


def _review_from_dict(value: dict[str, object]) -> HumanReviewCompleted:
    return HumanReviewCompleted(
        run_id=RunId(str(value["run_id"])),
        step_id=StepId(str(value["step_id"])),
        approved=bool(value["approved"]),
        summary=str(value.get("summary", "")),
    )


def _state_to_dict(state: RunState) -> dict[str, object]:
    return {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "workflow_id": state.workflow_id,
        "phase": state.phase.value,
        "repository": state.repository,
        "prompt": state.prompt,
        "workspace_id": state.workspace_id,
        "agent_runs": list(state.agent_runs),
        "max_agent_runs": state.max_agent_runs,
        "current_step_id": state.current_step_id,
        "current_agent_run_id": state.current_agent_run_id,
        "step_results": [_step_to_dict(step) for step in state.step_results],
        "human_review": (
            _review_to_dict(state.human_review) if state.human_review else None
        ),
        "failure_reason": state.failure_reason,
    }


def _state_from_dict(value: dict[str, object]) -> RunState:
    review = value.get("human_review")
    return RunState(
        run_id=RunId(str(value["run_id"])),
        task_id=TaskId(str(value["task_id"])),
        workflow_id=WorkflowId(str(value["workflow_id"])),
        phase=RunPhase(str(value["phase"])),
        repository=str(value.get("repository", "")),
        prompt=str(value.get("prompt", "")),
        workspace_id=(
            WorkspaceId(str(value["workspace_id"]))
            if value.get("workspace_id") is not None
            else None
        ),
        agent_runs=tuple(
            AgentRunId(str(agent_run_id))
            for agent_run_id in value.get("agent_runs", [])
        ),
        max_agent_runs=int(value.get("max_agent_runs", 3)),
        current_step_id=(
            StepId(str(value["current_step_id"]))
            if value.get("current_step_id") is not None
            else None
        ),
        current_agent_run_id=(
            AgentRunId(str(value["current_agent_run_id"]))
            if value.get("current_agent_run_id") is not None
            else None
        ),
        step_results=tuple(
            _step_from_dict(step)
            for step in value.get("step_results", [])
            if isinstance(step, dict)
        ),
        human_review=(
            _review_from_dict(review) if isinstance(review, dict) else None
        ),
        failure_reason=str(value.get("failure_reason", "")),
    )


def _event_to_dict(event: Event) -> dict[str, object]:
    if isinstance(event, StepCompleted):
        return {"type": "StepCompleted", **_step_to_dict(event)}
    if isinstance(event, HumanReviewCompleted):
        return {"type": "HumanReviewCompleted", **_review_to_dict(event)}
    if isinstance(event, RunRequested):
        return {
            "type": "RunRequested",
            "run_id": event.run_id,
            "task_id": event.task_id,
            "prompt": event.prompt,
            "repository": event.repository,
            "workflow_id": event.workflow_id,
        }
    if isinstance(event, WorkspaceProvisioned):
        return {
            "type": "WorkspaceProvisioned",
            "run_id": event.run_id,
            "workspace_id": event.workspace_id,
            "root_path": event.root_path,
        }
    if isinstance(event, AgentRunCompleted):
        return {
            "type": "AgentRunCompleted",
            "run_id": event.run_id,
            "agent_run_id": event.agent_run_id,
            "succeeded": event.succeeded,
            "summary": event.summary,
            "changed_files": list(event.changed_files),
        }
    if isinstance(event, ChangesPublished):
        return {
            "type": "ChangesPublished",
            "run_id": event.run_id,
            "review_url": event.review_url,
        }
    if isinstance(event, RunFailed):
        return {"type": "RunFailed", "run_id": event.run_id, "reason": event.reason}
    raise TypeError(f"cannot persist event {type(event).__name__}")


def _event_from_dict(value: dict[str, object]) -> Event:
    kind = value["type"]
    if kind == "StepCompleted":
        return _step_from_dict(value)
    if kind == "HumanReviewCompleted":
        return _review_from_dict(value)
    if kind == "RunRequested":
        return RunRequested(
            run_id=RunId(str(value["run_id"])),
            task_id=TaskId(str(value["task_id"])),
            prompt=str(value["prompt"]),
            repository=str(value["repository"]),
            workflow_id=WorkflowId(str(value["workflow_id"])),
        )
    if kind == "WorkspaceProvisioned":
        return WorkspaceProvisioned(
            run_id=RunId(str(value["run_id"])),
            workspace_id=WorkspaceId(str(value["workspace_id"])),
            root_path=str(value["root_path"]),
        )
    if kind == "AgentRunCompleted":
        return AgentRunCompleted(
            run_id=RunId(str(value["run_id"])),
            agent_run_id=AgentRunId(str(value["agent_run_id"])),
            succeeded=bool(value["succeeded"]),
            summary=str(value["summary"]),
            changed_files=tuple(str(path) for path in value.get("changed_files", [])),
        )
    if kind == "ChangesPublished":
        return ChangesPublished(
            run_id=RunId(str(value["run_id"])),
            review_url=str(value["review_url"]),
        )
    if kind == "RunFailed":
        return RunFailed(
            run_id=RunId(str(value["run_id"])), reason=str(value["reason"])
        )
    raise ValueError(f"unknown persisted event type {kind!r}")


__all__ = ["SQLiteStateStore"]
