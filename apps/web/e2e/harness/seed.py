"""Seed a completed WorkOrder and chat through the SQLite adapter.

The browser opens this file in a separate process to exercise cold-start
navigation, including a WorkOrder whose workflow is no longer installed.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import (
    AgentId, AgentInstanceId, ConversationId, Message,
    RunId, RunPhase, RunState, TaskId, WorkflowId,
)

CHAT_INSTANCE = AgentInstanceId("agi-seeded-chat")

async def seed(path: Path, repository: str) -> None:
    store = SQLiteStateStore(path)
    try:
        await _seed_chat(store)
        await store.save(RunState(
            run_id=RunId("run-seeded-history"),
            task_id=TaskId("task-seeded-history"),
            workflow_id=WorkflowId("retired-workflow"),
            phase=RunPhase.SUCCEEDED,
            repository=repository,
            prompt="Preserve browser navigation and durable conversation history.",
            name="Seeded navigation coverage",
        ))
    finally:
        store.close()


async def _seed_chat(store: SQLiteStateStore) -> None:
    await store.create_instance(
        AgentId("coder"),
        runner="claude",
        instance_id=CHAT_INSTANCE,
        conversation_id=ConversationId("conv-seeded-chat"),
    )
    await store.update_instance_metadata(
        CHAT_INSTANCE,
        title="Seeded SQLite conversation",
        archived=False,
        runner="claude",
    )
    await store.append_messages(
        CHAT_INSTANCE,
        (
            Message.user("What survives when the web process restarts?"),
            Message.assistant("The SQLite-backed conversation history survives."),
            Message.user("Can I still navigate back to this answer?"),
            Message.assistant("Yes. This second turn proves the complete history loaded."),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    asyncio.run(seed(Path(args.state) / "conversations.sqlite3", args.repository))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
