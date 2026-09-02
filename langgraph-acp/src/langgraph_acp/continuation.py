"""Picking a conversation back up in a process that never had it.

An agent asks to run a command. The person who has to answer is asleep. Keeping
a Python coroutine alive until they wake is the expensive way to wait, and it is
also the fragile way: the worker is redeployed, the pod is evicted, and the
conversation the agent was halfway through is gone along with the task that was
holding it.

It does not have to be. The ACP agent owns the transcript, the tool history and
the model context, and `session/load` is the protocol's way of asking for them
back. So the only thing that has to survive is the handful of strings that name
the conversation:

    ACPContinuation(agent="codex", session_id="sess_abc123", ...)
        | resume_continuation(...)
        v
    connect -> initialize -> session/load -> ACPSession

That is what this module is: the record, and the one call that turns it back
into a live session. It is here rather than in the caller because reconnecting
is this package's business -- an orchestrator that wrote its own `connect` and
`session/load` would have to be updated every time ACP's handshake changed, and
would be reimplementing the thing `resume_session` already is.

`metadata` is deliberately open. A continuation is written down by whatever is
orchestrating the agent, and that layer has its own identities to carry back --
which run, which node, which approval was outstanding. None of those mean
anything here, so they travel as JSON and are handed back unread.

What a continuation is *not* is conversation history. Nothing here is a
transcript, and a `to_dict` that grew one would hold a second version of the
conversation that could disagree with the agent's own. See
`langgraph_acp.store` for the same argument about bindings.
"""

from collections.abc import Mapping, Sequence
import os

from dataclasses import dataclass, field

from langgraph_acp._json import (
    JSONObject,
    JSONValue,
    as_mapping,
    as_optional_str,
    as_str,
    copied_mapping,
)
from langgraph_acp.agent import ACPAgentRegistry, default_registry
from langgraph_acp.client import ACPClient
from langgraph_acp.session import ACPSession


@dataclass(frozen=True, slots=True)
class ACPContinuation:
    """Everything needed to reach one conversation again, and nothing else.

    Serializable because the point of it is outliving the process: it goes into
    a database, a checkpoint or a queue message, and comes back somewhere else.
    """

    agent: str
    """The name the provider is registered under, which is what resolves it."""
    session_id: str
    """The opaque id the agent gave the conversation."""
    thread_id: str | None = None
    """The orchestrator's identity for the work, when it has one."""
    session_key: str | None = None
    """Which logical agent within that thread. See `langgraph_acp.store`."""
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    """Whatever the orchestrator needs handed back. Carried, never read."""

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("a continuation must name the agent to reconnect to")
        if not self.session_id:
            raise ValueError(
                "a continuation must name the session to load; without one there "
                "is nothing to resume and a fresh session would be a new task"
            )
        object.__setattr__(self, "metadata", copied_mapping(self.metadata))

    def to_dict(self) -> JSONObject:
        return {
            "agent": self.agent,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "session_key": self.session_key,
            "metadata": copied_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ACPContinuation":
        return cls(
            agent=as_str(data["agent"], field="agent"),
            session_id=as_str(data["session_id"], field="session_id"),
            thread_id=as_optional_str(data.get("thread_id"), field="thread_id"),
            session_key=as_optional_str(data.get("session_key"), field="session_key"),
            metadata=as_mapping(data.get("metadata"), field="metadata"),
        )


async def resume_continuation(
    continuation: ACPContinuation,
    *,
    registry: ACPAgentRegistry | None = None,
    cwd: str | os.PathLike[str] | None = None,
    mcp_servers: Sequence[Mapping[str, JSONValue]] = (),
) -> tuple[ACPClient, ACPSession]:
    """Reconnect to the agent and reload the conversation `continuation` names.

    The client comes back alongside the session because the caller owns both
    lifetimes: a session is a conversation on a connection, and closing the
    session leaves the process running. Close the client when the work is done.

    Raises `ACPAgentNotFoundError` when nothing is registered under the
    continuation's agent name, and `ACPAgentCapabilityError` when the agent
    cannot load a session -- before anything is sent, so a caller that must
    start over learns it without a half-open connection to clean up.
    """
    provider = (registry or default_registry()).resolve(continuation.agent)
    client = await provider.connect()
    try:
        session = await client.resume_session(
            continuation.session_id, cwd=cwd, mcp_servers=mcp_servers
        )
    except BaseException:
        await client.close()
        raise
    return client, session


__all__ = ["ACPContinuation", "resume_continuation"]
