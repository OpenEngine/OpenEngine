"""SQLite conversation persistence."""

import asyncio
import json
import sqlite3
import warnings

import pytest

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentRun,
    AgentRunId,
    AgentRunStatus,
    ApprovalId,
    ConversationId,
    Message,
    Role,
    RunId,
    RunOrigin,
    RunPhase,
    RunState,
    TaskId,
    ToolCall,
    WorkflowId,
)
from engine.ports import StateStore

CODER = AgentId("coder")


def test_sqlite_store_satisfies_the_port() -> None:
    store = SQLiteStateStore(":memory:")
    try:
        assert isinstance(store, StateStore)
    finally:
        store.close()


def test_conversation_survives_reopening_the_database(tmp_path) -> None:
    path = tmp_path / "conversations.sqlite3"
    first = SQLiteStateStore(path)
    instance = asyncio.run(first.create_instance(CODER))
    call = ToolCall(call_id="call-1", name="read", arguments='{"path":"README.md"}')
    asyncio.run(
        first.append_messages(
            instance.instance_id,
            (
                Message.user("what is here?"),
                Message.assistant(tool_calls=(call,)),
                Message.tool_result("call-1", "an engine"),
            ),
        )
    )
    first.close()

    second = SQLiteStateStore(path)
    try:
        loaded = asyncio.run(second.load_instance(instance.instance_id))
        conversation = asyncio.run(second.load_conversation(instance.instance_id))
    finally:
        second.close()

    assert loaded == instance
    assert conversation is not None
    assert [(message.role, message.content) for message in conversation.messages] == [
        (Role.USER, "what is here?"),
        (Role.ASSISTANT, ""),
        (Role.TOOL, "an engine"),
    ]
    assert conversation.messages[1].tool_calls == (call,)
    assert conversation.messages[2].tool_call_id == "call-1"
    assert len({message.message_id for message in conversation.messages}) == 3


def test_conversations_are_loaded_in_one_batch() -> None:
    store = SQLiteStateStore(":memory:")
    first = asyncio.run(store.create_instance(CODER))
    second = asyncio.run(store.create_instance(CODER))
    asyncio.run(store.append_messages(first.instance_id, (Message.user("First"),)))
    try:
        conversations = asyncio.run(
            store.load_conversations(
                (first.instance_id, second.instance_id, AgentInstanceId("missing"))
            )
        )
    finally:
        store.close()

    assert set(conversations) == {first.instance_id, second.instance_id}
    assert [message.content for message in conversations[first.instance_id].messages] == [
        "First"
    ]
    assert conversations[second.instance_id].messages == ()


def test_instance_metadata_survives_reopening_the_database(tmp_path) -> None:
    path = tmp_path / "conversations.sqlite3"
    first = SQLiteStateStore(path)
    instance = asyncio.run(first.create_instance(CODER, runner="codex"))
    asyncio.run(
        first.update_instance_metadata(
            instance.instance_id,
            title="Durable title",
            archived=True,
            runner="claude",
        )
    )
    first.close()

    second = SQLiteStateStore(path)
    try:
        loaded = asyncio.run(second.load_instance(instance.instance_id))
    finally:
        second.close()

    assert loaded is not None
    assert loaded.title == "Durable title"
    assert loaded.archived is True
    assert loaded.runner == "claude"


def test_existing_database_gets_default_instance_metadata(tmp_path) -> None:
    path = tmp_path / "conversations.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE agent_instances (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL UNIQUE,
            agent_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL UNIQUE,
            task_id TEXT,
            workspace_id TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO agent_instances (
            instance_id, agent_id, conversation_id, task_id, workspace_id
        ) VALUES ('agi-old', 'coder', 'conv-old', NULL, NULL)
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStateStore(path)
    try:
        loaded = asyncio.run(store.load_instance("agi-old"))
    finally:
        store.close()

    assert loaded is not None
    assert loaded.title == "New chat"
    assert loaded.archived is False
    assert loaded.runner == ""


def test_approvals_written_before_grants_existed_still_load(tmp_path) -> None:
    """A database from before approvals were bounded by a worktree, or paired
    with the call they were about.

    Null is the honest value for both: nobody recorded where one applied, so it
    matches no grant and is asked about again; nobody recorded which call it
    concerned, and nothing can work that out afterwards, so a client shows it at
    the end of its turn rather than beside a command it has guessed at.
    """
    path = tmp_path / "conversations.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE agent_instances (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL UNIQUE,
            agent_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL UNIQUE,
            task_id TEXT,
            workspace_id TEXT
        );

        CREATE TABLE approvals (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT NOT NULL UNIQUE,
            agent_run_id TEXT NOT NULL,
            instance_id TEXT NOT NULL REFERENCES agent_instances(instance_id),
            runner TEXT NOT NULL,
            kind TEXT NOT NULL,
            reason TEXT,
            command TEXT,
            cwd TEXT,
            tool_name TEXT,
            arguments TEXT,
            allowed_decisions TEXT NOT NULL,
            status TEXT NOT NULL,
            decision TEXT,
            decision_source TEXT,
            requested_at TEXT NOT NULL,
            decided_at TEXT
        );

        INSERT INTO agent_instances (
            instance_id, agent_id, conversation_id, task_id, workspace_id
        ) VALUES ('agi-old', 'coder', 'conv-old', NULL, NULL);

        INSERT INTO approvals (
            approval_id, agent_run_id, instance_id, runner, kind, command,
            allowed_decisions, status, requested_at
        ) VALUES ('apv-old', 'ar-old', 'agi-old', 'codex-app-server',
                  'command_execution', 'pytest', '["accept","cancel"]',
                  'pending', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStateStore(path)
    try:
        loaded = asyncio.run(store.load_approval(ApprovalId("apv-old")))
        grants = asyncio.run(store.list_session_grants())
    finally:
        store.close()

    assert loaded is not None
    assert loaded.command == "pytest"
    assert loaded.workspace_id is None
    assert loaded.tool_call_id is None
    assert grants == ()


def test_instances_are_newest_first_and_filterable() -> None:
    store = SQLiteStateStore(":memory:")
    try:
        first = asyncio.run(store.create_instance(CODER))
        second = asyncio.run(store.create_instance(CODER))
        other = asyncio.run(store.create_instance(AgentId("foreman")))

        assert asyncio.run(store.list_instances()) == (other, second, first)
        assert asyncio.run(store.list_instances(CODER)) == (second, first)
    finally:
        store.close()


def test_unknown_instances_refuse_messages() -> None:
    store = SQLiteStateStore(":memory:")
    try:
        with pytest.raises(KeyError):
            asyncio.run(store.append_messages("agi-nope", (Message.user("hello"),)))
    finally:
        store.close()


def test_agent_runs_are_upserted() -> None:
    store = SQLiteStateStore(":memory:")
    try:
        instance = asyncio.run(store.create_instance(CODER))
        running = AgentRun(
            agent_run_id=AgentRunId("ar-1"),
            instance_id=instance.instance_id,
            status=AgentRunStatus.RUNNING,
            runner="codex",
        )
        asyncio.run(store.record_agent_run(running))
        asyncio.run(
            store.record_agent_run(
                AgentRun(
                    agent_run_id=running.agent_run_id,
                    instance_id=instance.instance_id,
                    status=AgentRunStatus.SUCCEEDED,
                    summary="done",
                    changed_files=("README.md",),
                    runner="codex",
                )
            )
        )

        recorded = asyncio.run(store.agent_run(running.agent_run_id))
    finally:
        store.close()

    assert recorded is not None
    assert recorded.status is AgentRunStatus.SUCCEEDED
    assert recorded.summary == "done"
    assert recorded.changed_files == ("README.md",)


def test_a_work_order_and_its_conversations_survive_reopening(tmp_path) -> None:
    path = tmp_path / "runs.sqlite3"
    run_id = RunId("run-durable")
    state = RunState(
        run_id=run_id,
        task_id=TaskId("task-durable"),
        workflow_id=WorkflowId("durability-test"),
        phase=RunPhase.FAILED,
        repository="acme/api",
        prompt="Fix the race.",
        name="Fix shared counter race",
        failure_reason="the reviewer could not reach the repository",
        origin=RunOrigin(channel="C1", thread_id="17.5", author="U9"),
    )

    first = SQLiteStateStore(path)
    asyncio.run(first.save(state))
    asyncio.run(
        first.create_instance(
            AgentId("review-agent"),
            instance_id=AgentInstanceId("review-instance"),
            conversation_id=ConversationId("review-conversation"),
        )
    )
    first.close()

    second = SQLiteStateStore(path)
    try:
        loaded = asyncio.run(second.load(run_id))
        runs = asyncio.run(second.list_runs())
        instances = asyncio.run(second.list_instances())
    finally:
        second.close()

    assert loaded == state
    assert runs == (state,)
    assert instances[0].instance_id == "review-instance"
    assert instances[0].conversation_id == "review-conversation"


def test_legacy_human_review_phase_loads_as_running_without_a_warning(tmp_path) -> None:
    path = tmp_path / "runs.sqlite3"
    expected = RunState(
        run_id=RunId("run-legacy"),
        task_id=TaskId("task-legacy"),
        workflow_id=WorkflowId("implementation-review-v1"),
        phase=RunPhase.RUNNING_AGENT,
    )

    store = SQLiteStateStore(path)
    try:
        store._connection.execute(
            "INSERT INTO run_states (run_id, state_json) VALUES (?, ?)",
            (
                "run-legacy",
                json.dumps(
                    {
                        "run_id": "run-legacy",
                        "task_id": "task-legacy",
                        "workflow_id": "implementation-review-v1",
                        "phase": "awaiting_human_review",
                    }
                ),
            ),
        )
        store._connection.commit()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            loaded = asyncio.run(store.load(RunId("run-legacy")))
            runs = asyncio.run(store.list_runs())
    finally:
        store.close()

    assert loaded == expected
    assert runs == (expected,)


def test_a_row_this_build_cannot_read_is_skipped_rather_than_hiding_the_rest(
    tmp_path,
) -> None:
    """A run left by a build with a phase this one has never known.

    Reading the list is how every screen finds its WorkOrders, so one row it
    cannot make sense of must not take the others with it -- or the startup
    that reads the same list. It is warned about and left where it is.
    """
    path = tmp_path / "runs.sqlite3"
    current = RunState(
        run_id=RunId("run-current"),
        task_id=TaskId("task-current"),
        workflow_id=WorkflowId("implementation-review-rerank"),
    )

    store = SQLiteStateStore(path)
    try:
        asyncio.run(store.save(current))
        store._connection.execute(
            "INSERT INTO run_states (run_id, state_json) VALUES (?, ?)",
            (
                "run-legacy",
                json.dumps(
                    {
                        "run_id": "run-legacy",
                        "task_id": "task-legacy",
                        "workflow_id": "implementation-review-v1",
                        "phase": "future_phase",
                    }
                ),
            ),
        )
        store._connection.commit()

        with pytest.raises(ValueError, match="future_phase"):
            asyncio.run(store.load(RunId("run-legacy")))
        with pytest.warns(RuntimeWarning, match="skipping incompatible workflow run"):
            runs = tuple(asyncio.run(store.list_runs()))
    finally:
        store.close()

    assert runs == (current,)
