"""Talking to an agent: the mechanics around one turn.

`AgentRunner` answers a question; it does not remember, persist, or decide what
the agent is allowed to use. Those are execution mechanics, which belong to the
runtime -- so this is where loading history, appending to it, recording the run,
and resolving a profile's grants happen. A caller (a UI, an HTTP handler, a
dispatched command) gets one method: say something, get a turn back.

Keeping it here rather than in the interface means the Streamlit page and the
control server cannot drift into two subtly different notions of what a
conversation is.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from uuid import uuid4

from engine.domain.agents import AgentInstance, AgentProfile, AgentRun, AgentRunStatus
from engine.domain.chat import Message
from engine.domain.ids import AgentId, AgentInstanceId, AgentRunId, TaskId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import AgentTurn
from engine.runtime.capabilities import Capabilities
from engine.runtime.profiles import BUILT_IN, profile_for

#: Grant name -> the tool it resolves to. Empty until tools exist; a profile
#: granting anything therefore fails loudly, which is the intended behaviour.
NO_TOOLS: Mapping[str, ToolSpec] = {}


class UnknownInstanceError(KeyError):
    """The store has never heard of that instance."""

    def __init__(self, instance_id: AgentInstanceId) -> None:
        super().__init__(f"no agent instance {instance_id!r}")
        self.instance_id = instance_id


class UnknownToolGrantError(RuntimeError):
    """A profile grants a tool nothing can provide.

    Refusing beats running the agent anyway: a foreman that cannot dispatch is
    not a foreman, and discovering that from its answers rather than from an
    exception wastes everybody's time.
    """

    def __init__(self, agent_id: AgentId, missing: Sequence[str]) -> None:
        super().__init__(
            f"profile {agent_id!r} grants {list(missing)}, which resolve to no tool"
        )
        self.agent_id = agent_id
        self.missing = tuple(missing)


class AgentSession:
    """Conversations with agents, over a wired capability set."""

    def __init__(
        self,
        capabilities: Capabilities,
        profiles: Mapping[AgentId, AgentProfile] = BUILT_IN,
        tools: Mapping[str, ToolSpec] = NO_TOOLS,
    ) -> None:
        self._capabilities = capabilities
        self._profiles = profiles
        self._tools = tools

    @property
    def profiles(self) -> Mapping[AgentId, AgentProfile]:
        return self._profiles

    async def start(self, agent_id: AgentId, task_id: TaskId | None = None) -> AgentInstance:
        """Begin a conversation with an agent. Fails before touching the store
        if the profile is unknown."""
        profile_for(agent_id, self._profiles)
        return await self._capabilities.state_store.create_instance(agent_id, task_id)

    async def instances(self, agent_id: AgentId | None = None) -> Sequence[AgentInstance]:
        return await self._capabilities.state_store.list_instances(agent_id)

    async def history(self, instance_id: AgentInstanceId) -> tuple[Message, ...]:
        conversation = await self._capabilities.state_store.load_conversation(instance_id)
        if conversation is None:
            raise UnknownInstanceError(instance_id)
        return conversation.messages

    async def say(self, instance_id: AgentInstanceId, text: str) -> AgentTurn:
        """Add a message to the conversation and get the agent's reply.

        The user's message is stored before the agent runs, so a failed turn
        leaves an accurate transcript -- a question with no answer -- rather
        than losing what was asked.
        """
        store = self._capabilities.state_store

        instance = await store.load_instance(instance_id)
        if instance is None:
            raise UnknownInstanceError(instance_id)
        conversation = await store.load_conversation(instance_id)
        if conversation is None:
            raise UnknownInstanceError(instance_id)

        profile = profile_for(instance.agent_id, self._profiles)
        tools = self._tools_for(profile)

        question = Message.user(text)
        await store.append_messages(instance_id, (question,))

        agent_run = AgentRun(
            agent_run_id=_new_agent_run_id(),
            instance_id=instance_id,
            status=AgentRunStatus.RUNNING,
        )
        await store.record_agent_run(agent_run)

        try:
            turn = await self._capabilities.agent_runner.run_turn(
                agent_run.agent_run_id,
                profile,
                (*conversation.messages, question),
                tools=tools,
                workspace_id=instance.workspace_id,
            )
        except Exception as error:
            await store.record_agent_run(
                replace(
                    agent_run,
                    status=AgentRunStatus.FAILED,
                    summary=f"{type(error).__name__}: {error}",
                )
            )
            raise

        await store.append_messages(instance_id, (turn.message,))
        await store.record_agent_run(
            replace(
                agent_run,
                status=AgentRunStatus.SUCCEEDED,
                summary=turn.message.content,
            )
        )
        return turn

    def _tools_for(self, profile: AgentProfile) -> tuple[ToolSpec, ...]:
        missing = [grant for grant in profile.capabilities if grant not in self._tools]
        if missing:
            raise UnknownToolGrantError(profile.agent_id, missing)
        return tuple(self._tools[grant] for grant in profile.capabilities)


def _new_agent_run_id() -> AgentRunId:
    """Mint an id for one execution.

    Random, which is fine for an interactive session and wrong under a durable
    workflow runtime: replay has to see the id it saw the first time. When runs
    are started durably the id comes in on the command instead -- which is why
    `StartAgentRun` already carries one.
    """
    return AgentRunId(f"ar-{uuid4().hex[:12]}")


__all__ = ["AgentSession", "UnknownInstanceError", "UnknownToolGrantError"]
