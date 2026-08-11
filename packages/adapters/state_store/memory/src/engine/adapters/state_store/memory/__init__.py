"""State Store capability, held in process.

Unlike its Postgres sibling this one is finished: every method does what it says.
What it does not do is survive the process, which is the whole of the difference
between the two and the reason both exist. Chatting with an agent needs a real
store today; nothing about the conversation needs to outlive a restart yet.

It is also the honest test double. A fake that only pretends to store things
lets a caller's ordering bug pass unnoticed, so this keeps insertion order,
mints ids, and rejects writes against instances it never created -- the same
things Postgres will do.

Implements `engine.ports.StateStore`.
"""

from collections.abc import Sequence
from itertools import count
from threading import Lock
from uuid import uuid4

from engine.domain.agents import AgentInstance, AgentRun
from engine.domain.chat import Conversation, Message
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


class InMemoryStateStore:
    """Everything in dicts, guarded by a lock.

    A plain `threading.Lock` rather than an `asyncio.Lock`: callers reach this
    through `asyncio.run`, which means a fresh event loop per call, and an
    asyncio primitive bound to a dead loop is a bug waiting for the second
    caller. No method awaits while holding it.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._states: dict[RunId, RunState] = {}
        self._events: dict[RunId, list[Event]] = {}
        self._instances: dict[AgentInstanceId, AgentInstance] = {}
        self._conversations: dict[AgentInstanceId, Conversation] = {}
        self._agent_runs: dict[AgentRunId, AgentRun] = {}
        self._message_numbers = count(1)

    # --- runs ------------------------------------------------------------

    async def load(self, run_id: RunId) -> RunState | None:
        with self._lock:
            return self._states.get(run_id)

    async def save(self, state: RunState) -> None:
        with self._lock:
            self._states[state.run_id] = state

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        with self._lock:
            self._events.setdefault(run_id, []).extend(events)

    async def history(self, run_id: RunId) -> Sequence[Event]:
        with self._lock:
            return tuple(self._events.get(run_id, ()))

    # --- agent identity and conversation ---------------------------------

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
    ) -> AgentInstance:
        instance = AgentInstance(
            instance_id=AgentInstanceId(f"agi-{uuid4().hex[:12]}"),
            agent_id=agent_id,
            conversation_id=ConversationId(f"conv-{uuid4().hex[:12]}"),
            task_id=task_id,
            workspace_id=workspace_id,
        )
        with self._lock:
            self._instances[instance.instance_id] = instance
            # An instance without its conversation is a state no reader should
            # have to handle, so the two are created together or not at all.
            self._conversations[instance.instance_id] = Conversation(
                conversation_id=instance.conversation_id,
                instance_id=instance.instance_id,
            )
        return instance

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        with self._lock:
            return self._instances.get(instance_id)

    async def list_instances(self, agent_id: AgentId | None = None) -> Sequence[AgentInstance]:
        with self._lock:
            instances = list(self._instances.values())
        if agent_id is not None:
            instances = [i for i in instances if i.agent_id == agent_id]
        return tuple(reversed(instances))  # newest first

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        with self._lock:
            return self._conversations.get(instance_id)

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        with self._lock:
            conversation = self._conversations.get(instance_id)
            if conversation is None:
                # An integrity check, not a lookup: callers resolve the instance
                # first, so reaching here means messages were about to pile up
                # somewhere no reader would ever look.
                raise KeyError(f"no agent instance {instance_id!r}")
            stamped = tuple(
                message.with_id(MessageId(f"msg-{next(self._message_numbers):06d}"))
                for message in messages
            )
            self._conversations[instance_id] = conversation.appending(*stamped)

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        with self._lock:
            self._agent_runs[agent_run.agent_run_id] = agent_run

    # --- beyond the port --------------------------------------------------

    async def agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        """Read back one execution. Not on the port -- nothing needs it there
        yet, and a port method with one in-memory implementation is a guess."""
        with self._lock:
            return self._agent_runs.get(agent_run_id)


__all__ = ["InMemoryStateStore"]
