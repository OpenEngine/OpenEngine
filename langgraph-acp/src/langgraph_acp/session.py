"""Session identity, and the live conversation it names.

Two types, and the distinction between them is load-bearing:

    ACPSessionSpec  configuration -- how a node should choose a conversation
        |
        v
    ACPSession      the conversation itself -- an id, and a turn to run in it

Neither holds conversation history. The ACP agent owns that, and asking it to
resume `sess_abc123` is what restores it; this package only remembers the opaque
identifier needed to ask. That is the whole reason a reply arriving on a webhook
days later needs no transcript reconstruction.

The identity a store persists is `(thread_id, session_key)` rather than
`thread_id` alone, because one LangGraph thread routinely runs several agents --
an implementer and three reviewers on the same pull request -- and none of them
may resume another's conversation. Writing that mapping down is
`ACPSessionStore`, in `langgraph_acp.store`; what lives here is the intent that
selects a conversation, and the session an id names once one has been resolved.
"""

from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable

from langgraph_acp._json import JSONValue
from langgraph_acp.events import ACPEvent

#: What a turn can be prompted with: plain text, or ACP content blocks verbatim.
#: Text is the case every agent must support, so it is the case worth spelling
#: `session.prompt("Review this change")`; anything richer -- images, embedded
#: file context -- is passed through as the blocks the protocol defines.
ACPPrompt: TypeAlias = str | Sequence[Mapping[str, JSONValue]]


class ACPSessionStrategy(StrEnum):
    """How a node decides between starting and continuing a conversation.

    A `StrEnum` so `ACPSessionSpec(strategy="reuse")` -- the documented spelling
    -- is the same value as `ACPSessionStrategy.REUSE`, while a typo still fails
    at construction instead of at the first prompt.
    """

    NEW = "new"
    """Always start a fresh ACP session, whatever the store already holds."""

    REUSE = "reuse"
    """Resume the bound session if there is one, otherwise create and bind it."""

    RESUME = "resume"
    """Require an existing session; absent one, fail rather than start over."""


@dataclass(frozen=True, slots=True)
class ACPSessionSpec:
    """How a node identifies the conversation it prompts.

    `reuse` is the default because it is what a durable agent normally wants:
    the first invocation creates a conversation, every later one continues it.

    A `key` of `None` means "name this conversation after the node", which is
    right until one node speaks for more than one logical agent -- then the key
    is given explicitly.
    """

    strategy: ACPSessionStrategy | str = ACPSessionStrategy.REUSE
    """`ACPSessionStrategy.REUSE` or its spelling, `"reuse"`. Stored as the enum."""
    key: str | None = None
    """Logical name of the conversation within its thread."""
    session_id: str | None = None
    """An ACP session to resume outright, bypassing the store's binding."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy", ACPSessionStrategy(self.strategy))
        if self.session_id is not None and self.strategy is ACPSessionStrategy.NEW:
            raise ValueError(
                "strategy='new' always starts a fresh session, so session_id "
                f"{self.session_id!r} could only be silently ignored"
            )


@runtime_checkable
class ACPSession(Protocol):
    """One live agent conversation, and the operations scoped to it.

    Obtained from `ACPClient.new_session` or `ACPClient.resume_session`, never
    constructed directly: a session id is a fact the agent produces, and a
    session object that invented one would name nothing.

    A turn is a stream rather than a return value, because the interesting part
    of an ACP turn happens while it runs -- tool calls, message deltas, plan
    revisions -- and a caller that only sees the end has watched none of it:

        async for event in session.prompt("Review this change"):
            ...

    The stream ends with `acp.prompt.completed`. Stopping early leaves the agent
    working, so either `cancel()` first or close the stream -- `async with
    contextlib.aclosing(session.prompt(...)) as turn:` -- which cancels for you.
    That closing cancels is why the return type is `AsyncGenerator` rather than
    the `AsyncIterator` it would otherwise be: `aclose` is part of the contract,
    not an implementation detail a consumer happens to be able to reach.
    """

    @property
    def session_id(self) -> str:
        """The opaque identifier the agent gave this conversation."""
        ...

    def prompt(self, prompt: ACPPrompt) -> AsyncGenerator[ACPEvent, None]:
        """Run one turn, streaming what happens in it as it happens."""
        ...

    async def cancel(self) -> None:
        """Stop the turn in flight. The conversation itself survives."""
        ...

    async def close(self) -> None:
        """Release this conversation's local resources.

        Distinct from `cancel`: cancelling stops a turn, closing gives up the
        session. Nothing is destroyed on the agent's side -- ACP has no method
        for that today -- so a closed session id can still be resumed later.
        """
        ...


__all__ = ["ACPPrompt", "ACPSession", "ACPSessionSpec", "ACPSessionStrategy"]
