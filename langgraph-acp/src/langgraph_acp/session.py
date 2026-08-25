"""Session identity: which agent conversation a node is talking to.

Three types, and the distinction between them is load-bearing:

    ACPSession         configuration -- how a node should choose a conversation
        |
        v
    ACPSessionBinding  the persisted fact -- (thread, key) -> ACP session id
        |
        v
    ACPSessionRef      the conversation a completed turn actually used

None of them holds conversation history. The ACP agent owns that, and asking it
to resume `sess_abc123` is what restores it; this package only remembers the
opaque identifier needed to ask. That is the whole reason a reply arriving on a
webhook days later needs no transcript reconstruction.

The identity is `(thread_id, session_key)` rather than `thread_id` alone because
one LangGraph thread routinely runs several agents -- an implementer and three
reviewers on the same pull request -- and none of them may resume another's
conversation.
"""

from dataclasses import dataclass
from enum import StrEnum

from langgraph_acp._json import JSONObject


class ACPSessionStrategy(StrEnum):
    """How a node decides between starting and continuing a conversation.

    A `StrEnum` so `ACPSession(strategy="reuse")` -- the documented spelling --
    is the same value as `ACPSessionStrategy.REUSE`, while a typo still fails at
    construction instead of at the first prompt.
    """

    NEW = "new"
    """Always start a fresh ACP session, whatever the store already holds."""

    REUSE = "reuse"
    """Resume the bound session if there is one, otherwise create and bind it."""

    RESUME = "resume"
    """Require an existing session; absent one, fail rather than start over."""


@dataclass(frozen=True, slots=True)
class ACPSession:
    """How a node identifies the conversation it prompts.

    `reuse` is the default because it is what a durable agent normally wants:
    the first invocation creates a conversation, every later one continues it.

    A `key` of `None` means "name this conversation after the node", which is
    right until one node speaks for more than one logical agent -- then the key
    is given explicitly.
    """

    strategy: ACPSessionStrategy = ACPSessionStrategy.REUSE
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


@dataclass(frozen=True, slots=True)
class ACPSessionRef:
    """The conversation a turn actually used, reported back with its result.

    Distinct from `ACPSession`, which only expresses an intent: this names a
    session that exists, so a caller can record it, display it, or hand it to a
    later invocation.
    """

    agent: str
    session_id: str
    key: str

    def to_dict(self) -> JSONObject:
        return {"agent": self.agent, "session_id": self.session_id, "key": self.key}

    @classmethod
    def from_dict(cls, data: JSONObject) -> "ACPSessionRef":
        return cls(
            agent=str(data["agent"]),
            session_id=str(data["session_id"]),
            key=str(data["key"]),
        )


@dataclass(frozen=True, slots=True)
class ACPSessionBinding:
    """The durable mapping from LangGraph identity to an ACP session id.

    This is the entire contents of an `ACPSessionStore` row. It is not agent
    memory: no messages, no tool history, no model context. Losing it costs the
    ability to *find* a conversation, not the conversation itself.

    The field names are the store's vocabulary rather than `ACPSessionRef`'s --
    a row keyed by `(thread_id, session_key)` reads better under those names --
    and `ref` bridges the two so no caller has to rename by hand.
    """

    thread_id: str
    session_key: str
    agent: str
    acp_session_id: str

    @property
    def ref(self) -> ACPSessionRef:
        """The same session, named the way a result names it."""
        return ACPSessionRef(
            agent=self.agent, session_id=self.acp_session_id, key=self.session_key
        )

    def to_dict(self) -> JSONObject:
        return {
            "thread_id": self.thread_id,
            "session_key": self.session_key,
            "agent": self.agent,
            "acp_session_id": self.acp_session_id,
        }

    @classmethod
    def from_dict(cls, data: JSONObject) -> "ACPSessionBinding":
        return cls(
            thread_id=str(data["thread_id"]),
            session_key=str(data["session_key"]),
            agent=str(data["agent"]),
            acp_session_id=str(data["acp_session_id"]),
        )


__all__ = ["ACPSession", "ACPSessionBinding", "ACPSessionRef", "ACPSessionStrategy"]
