"""Small session doubles shared by web fixtures and endpoint tests."""

import asyncio
from collections.abc import Sequence
from engine.domain import AgentProfile, AgentRunId, Message
from engine.ports import AgentTurn, Workspace, WorkspaceState
from engine.ports import Message as CommunicationsMessage
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


class ConcurrentRunner:
    """A controllably slow runner that records how much work overlaps."""

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, replies: Sequence[str] = ("ok",)) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[Message, ...]] = []
        self.workspace_ids: list[str | None] = []
        self.active = 0
        self.most_active = 0

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools=(),
        workspace_id=None,
    ) -> AgentTurn:
        self.seen.append(tuple(messages))
        self.workspace_ids.append(workspace_id)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        reply = self.replies.pop(0) if self.replies else "ok"
        return AgentTurn(Message.assistant(reply))

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        pass


class ConversationWorkspaces:
    """A provider whose checkouts come and go, as real ones do."""

    def __init__(self) -> None:
        self.count = 0
        self.detached: set[str] = set()
        self.attachments: list[tuple[str, str, str]] = []

    async def provision(
        self, repository: str, base_ref: str, *, co_author: str = ""
    ) -> Workspace:
        self.count += 1
        return self._workspace(f"ws-{self.count}", repository, base_ref)

    async def root_path(self, workspace_id: str) -> str:
        if workspace_id in self.detached:
            message = f"no workspace {workspace_id!r}"
            raise KeyError(message)
        return f"/worktrees/{workspace_id}"

    async def state(self, workspace_id: str) -> WorkspaceState:
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=f"engine/{workspace_id}",
            root_path=(
                None if workspace_id in self.detached else f"/worktrees/{workspace_id}"
            ),
        )

    async def attach(
        self, workspace_id: str, repository: str, base_ref: str, *, co_author: str = ""
    ) -> Workspace:
        self.attachments.append((workspace_id, repository, base_ref))
        self.detached.discard(workspace_id)
        return self._workspace(workspace_id, repository, base_ref)

    async def detach(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    async def dispose(self, workspace_id: str) -> None:
        self.detached.add(workspace_id)

    def _workspace(
        self, workspace_id: str, repository: str, base_ref: str
    ) -> Workspace:
        return Workspace(
            workspace_id=workspace_id,
            root_path=f"/worktrees/{workspace_id}",
            repository=repository,
            base_ref=base_ref,
            ref=f"engine/{workspace_id}",
        )


class RecordingCommunications:
    """A chat provider that remembers what was said and where."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, CommunicationsMessage | str, str]] = []

    async def post(self, channel, message, run_id=None, thread_id="") -> str:
        self.posts.append((channel, message, thread_id))
        return "1700.0002"

    async def reply(self, message_id: str, message: str) -> str:  # pragma: no cover
        raise NotImplementedError


class GreetingRunner:
    """A minimal runner that satisfies ``McpAgentRunner`` for tests.

    Returns a canned greeting from the concierge without calling any tools.
    """

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    async def run_turn(
        self, agent_run_id, profile, messages, tools=(), workspace_id=None
    ):
        return AgentTurn(message=Message.assistant("Hi, how can I help?"))

    async def run_turn_with_mcp(
        self, agent_run_id, profile, messages, mcp_server, workspace_id=None
    ):
        return AgentTurn(message=Message.assistant("Hi, how can I help?"))

    async def cancel(self, agent_run_id):
        pass


class WebStores:
    """Separate conversation and graph stores plus explicit checkpoint fixtures."""

    def __init__(self):
        from engine.adapters.state_store.memory import InMemoryStateStore
        from engine.graph_runtime_langgraph.store import InMemoryGraphRuntimeStore
        from engine.runtime.run_read_model import RunReader
        self.state = InMemoryStateStore()
        self.graph_store = InMemoryGraphRuntimeStore()
        self.runs = RunReader()
        self.runs.store = self.graph_store
        self.runs.runtime = self
        self.snapshots = {}

    def __getattr__(self, name):
        return getattr(self.state, name)

    async def snapshot(self, run_id):
        return self.snapshots.get(run_id)

    async def seed_run(self, state):
        from engine.domain import RunPhase
        from engine.graph_runtime import RunSnapshot, RunStatus
        from engine.graph_runtime.topology import GraphId
        self.snapshots[state.run_id] = RunSnapshot(
            run_id=state.run_id, graph_id=GraphId(str(state.workflow_id)),
            status={
                RunPhase.SCHEDULED: RunStatus.SCHEDULED,
                RunPhase.PENDING: RunStatus.RUNNING,
                RunPhase.RUNNING_AGENT: RunStatus.RUNNING,
                RunPhase.SUCCEEDED: RunStatus.COMPLETED,
                RunPhase.FAILED: RunStatus.FAILED,
            }[state.phase], error=state.failure_reason,
        )
        await self.runs.save(state)

    def bind(self, runtime, *, seeded_snapshots=True):
        runtime.store = self.graph_store
        if seeded_snapshots:
            runtime.seeded_snapshots = self.snapshots
        self.runs.runtime = runtime
