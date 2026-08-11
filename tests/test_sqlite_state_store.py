"""SQLite conversation persistence."""

import asyncio

import pytest

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import AgentId, AgentRun, AgentRunId, AgentRunStatus, Message, Role, ToolCall
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
