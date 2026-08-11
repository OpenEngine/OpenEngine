"""State Store capability.

Durable persistence of run state, agent identity, and conversation history.
Postgres is the intended first implementation; an in-memory dict satisfies it
for tests.

`append_events` plus `load` is deliberately event-sourcing-shaped: state can
always be rebuilt by folding history through `engine.core.decide`.

Conversations live here rather than inside a model provider's session. An
adapter may keep a native session for efficiency, but if the store is not the
source of truth then history cannot be resumed after a restart, inspected by a
human, or moved to a different provider.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from engine.domain.agents import AgentInstance, AgentRun
from engine.domain.chat import Conversation, Message
from engine.domain.events import Event
from engine.domain.ids import AgentId, AgentInstanceId, RunId, TaskId, WorkspaceId
from engine.domain.state import RunState


@runtime_checkable
class StateStore(Protocol):
    """Persists run state, the events that produced it, and agent history."""

    async def load(self, run_id: RunId) -> RunState | None:
        """Return the stored state, or None if the run is unknown."""
        ...

    async def save(self, state: RunState) -> None:
        ...

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        ...

    async def history(self, run_id: RunId) -> Sequence[Event]:
        ...

    # --- agent identity and conversation ---------------------------------

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentInstance:
        """Start a durable instance of an agent role, with an empty conversation.

        The store mints both ids, so an instance and its conversation cannot
        exist without each other. A caller-provisioned workspace may be attached
        atomically at creation.
        """
        ...

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        ...

    async def list_instances(self, agent_id: AgentId | None = None) -> Sequence[AgentInstance]:
        """Every instance, or every instance of one role. Newest first."""
        ...

    async def set_instance_title(
        self, instance_id: AgentInstanceId, title: str
    ) -> None:
        """Persist the human-readable title for an instance's conversation."""
        ...

    async def set_instance_archived(
        self, instance_id: AgentInstanceId, archived: bool
    ) -> None:
        """Persist whether an instance's conversation is archived."""
        ...

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        """The instance's history, or None if the instance is unknown.

        Keyed by instance rather than conversation id: an instance owns exactly
        one conversation, and callers hold the instance.
        """
        ...

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        """Append to the instance's conversation, in order. The store assigns
        each message an id."""
        ...

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        """Upsert one execution's status and outcome, keyed by its id."""
        ...


__all__ = ["StateStore"]
