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
from engine.ports.agent_runner import (
    AgentRunner,
    AgentTurn,
    MessageCallback,
    StreamingAgentRunner,
)
from engine.runtime.capabilities import Capabilities
from engine.runtime.profiles import BUILT_IN, profile_for

#: Grant name -> the tool it resolves to. Empty until tools exist; a profile
#: granting anything therefore fails loudly, which is the intended behaviour.
NO_TOOLS: Mapping[str, ToolSpec] = {}

#: What the single wired runner is called when the composition root does not
#: name several.
DEFAULT_RUNNER = "default"


class UnknownInstanceError(KeyError):
    """The store has never heard of that instance."""

    def __init__(self, instance_id: AgentInstanceId) -> None:
        super().__init__(f"no agent instance {instance_id!r}")
        self.instance_id = instance_id


class UnknownRunnerError(KeyError):
    """No runner is registered under that name."""

    def __init__(self, name: str, known: Sequence[str]) -> None:
        super().__init__(f"no runner {name!r}; wired: {sorted(known)}")
        self.name = name


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
        runners: Mapping[str, AgentRunner] | None = None,
    ) -> None:
        """`runners` lets one process offer a choice of agent runner.

        The names are the composition root's to invent and mean nothing here --
        the same arrangement as tool grants. Omit it and the single runner from
        `Capabilities` is used, which is what the dispatcher does for runs that
        nobody is sitting in front of.

        Switching runner mid-conversation is allowed, and is the point: we hold
        the transcript, so whichever one answers next is handed everything that
        came before it, including what the other one did.
        """
        self._capabilities = capabilities
        self._profiles = profiles
        self._tools = tools
        self._runners: Mapping[str, AgentRunner] = (
            dict(runners) if runners else {DEFAULT_RUNNER: capabilities.agent_runner}
        )

    @property
    def profiles(self) -> Mapping[AgentId, AgentProfile]:
        return self._profiles

    @property
    def runners(self) -> tuple[str, ...]:
        """The runner names this process offers, in the order it wired them."""
        return tuple(self._runners)

    @property
    def default_runner(self) -> str:
        return next(iter(self._runners))

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

    async def say(
        self,
        instance_id: AgentInstanceId,
        text: str,
        runner: str | None = None,
        on_message: MessageCallback | None = None,
    ) -> AgentTurn:
        """Add a message to the conversation and get the agent's reply.

        The user's message is stored before the agent runs, so a failed turn
        leaves an accurate transcript -- a question with no answer -- rather
        than losing what was asked.

        `runner` names which one answers, defaulting to the first wired.
        When supplied, `on_message` receives transcript messages as a streaming
        runner produces them. A non-streaming runner remains valid and reports
        the same messages through the callback once its turn completes.
        """
        runner_name = runner or self.default_runner
        if runner_name not in self._runners:
            raise UnknownRunnerError(runner_name, self.runners)
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
            runner=runner_name,
        )
        await store.record_agent_run(agent_run)

        try:
            selected_runner = self._runners[runner_name]
            arguments = (
                agent_run.agent_run_id,
                profile,
                (*conversation.messages, question),
            )
            if on_message is not None and isinstance(selected_runner, StreamingAgentRunner):
                turn = await selected_runner.run_turn_stream(
                    *arguments,
                    on_message=on_message,
                    tools=tools,
                    workspace_id=instance.workspace_id,
                )
            else:
                turn = await selected_runner.run_turn(
                    *arguments,
                    tools=tools,
                    workspace_id=instance.workspace_id,
                )
                if on_message is not None:
                    for message in turn.transcript:
                        await on_message(message)
        except Exception as error:
            await store.record_agent_run(
                replace(
                    agent_run,
                    status=AgentRunStatus.FAILED,
                    summary=f"{type(error).__name__}: {error}",
                )
            )
            raise

        # The whole turn, not just the conclusion: an agent that read three
        # files before answering leaves those reads in the transcript, so a
        # later reader can see why it said what it said.
        await store.append_messages(instance_id, turn.transcript)
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


__all__ = [
    "DEFAULT_RUNNER",
    "AgentSession",
    "UnknownInstanceError",
    "UnknownRunnerError",
    "UnknownToolGrantError",
]
