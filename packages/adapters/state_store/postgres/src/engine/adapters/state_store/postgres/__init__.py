"""State Store capability, backed by Postgres.

Placeholder for Ticket 1. Satisfies `engine.ports.StateStore` structurally; no
driver, connection pool, schema, or migrations yet.
"""

from collections.abc import Sequence

from engine.domain.agents import AgentInstance, AgentRun
from engine.domain.chat import Conversation, Message
from engine.domain.events import Event
from engine.domain.ids import AgentId, AgentInstanceId, RunId, TaskId, WorkspaceId
from engine.domain.state import RunState


class PostgresStateStore:
    """Persists run state, agent identity, and conversations in Postgres.

    Implements `engine.ports.StateStore`.
    """

    def __init__(self, dsn: str, schema: str = "engine") -> None:
        self._dsn = dsn
        self._schema = schema

    async def load(self, run_id: RunId) -> RunState | None:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def save(self, state: RunState) -> None:
        raise NotImplementedError("Postgres writes land with the state-store ticket")

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        raise NotImplementedError("Event append lands with the state-store ticket")

    async def history(self, run_id: RunId) -> Sequence[Event]:
        raise NotImplementedError("History reads land with the state-store ticket")

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentInstance:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def list_instances(self, agent_id: AgentId | None = None) -> Sequence[AgentInstance]:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        raise NotImplementedError("Conversation reads land with the state-store ticket")

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        raise NotImplementedError("Conversation writes land with the state-store ticket")

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        raise NotImplementedError("Agent run records land with the state-store ticket")


__all__ = ["PostgresStateStore"]
