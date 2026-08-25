"""Streamed activity, normalized.

An ACP turn produces far more than a workflow should checkpoint: token deltas,
tool progress, plan revisions, usage updates. Writing that into durable
LangGraph state would make every keystroke a state transition, so it is streamed
as events instead and only the outcome is durable.

Event names are namespaced -- `acp.tool.updated` -- so a consumer subscribed to
a mixed stream can select this package's events without knowing them all. The
`type` field holds the unnamespaced remainder, and `name` renders the full form.

`ACPEventType` lists the vocabulary but does not constrain the field. An agent
update this package does not recognize becomes `acp.raw` and reaches the
consumer intact; a version of this library that crashes on an ACP addition
would be worse than one that passes it along uninterpreted.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from langgraph_acp._json import (
    JSONObject,
    JSONValue,
    as_mapping,
    as_optional_str,
    as_str,
    copied_mapping,
)

#: Prefix distinguishing this package's events in a shared stream.
EVENT_NAMESPACE = "acp"


class ACPEventType(StrEnum):
    """The event names this package emits, without their namespace."""

    SESSION_STARTED = "session.started"
    SESSION_RESUMED = "session.resumed"
    SESSION_CLOSED = "session.closed"
    SESSION_INFO_UPDATED = "session.info_updated"

    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    THOUGHT_DELTA = "thought.delta"

    TOOL_STARTED = "tool.started"
    TOOL_UPDATED = "tool.updated"
    TOOL_COMPLETED = "tool.completed"

    PLAN_UPDATED = "plan.updated"

    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_RESOLVED = "permission.resolved"

    ELICITATION_REQUESTED = "elicitation.requested"
    ELICITATION_RESOLVED = "elicitation.resolved"

    CONFIG_UPDATED = "config.updated"
    USAGE_UPDATED = "usage.updated"
    PROMPT_COMPLETED = "prompt.completed"

    ERROR = "error"
    RAW = "raw"
    """An agent update with no normalized form. The forward-compatibility hatch."""


@dataclass(frozen=True, slots=True)
class ACPEvent:
    """One thing that happened during a turn, with enough context to place it.

    The four identity fields answer "whose event is this?" from a stream that
    mixes agents and threads. Only `agent` is required; a session id does not
    exist yet when a session fails to start, and a bare ACP client has no thread
    or node at all.
    """

    agent: str
    type: str
    """An `ACPEventType` value, or an unrecognized name passed through."""
    session_id: str | None = None
    thread_id: str | None = None
    node: str | None = None
    """The LangGraph node that produced it."""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    data: Mapping[str, JSONValue] = field(default_factory=dict)
    """The payload, shaped by `type`. Copied, nested containers included."""

    def __post_init__(self) -> None:
        # Accept either spelling of the name. A consumer matching on `acp.error`
        # and a producer writing `error` mean the same event, and the difference
        # should not survive as far as an equality check.
        object.__setattr__(
            self, "type", str(self.type).removeprefix(f"{EVENT_NAMESPACE}.")
        )
        if self.timestamp.tzinfo is None:
            raise ValueError(
                "timestamp must be timezone-aware: events from several workers "
                "are ordered against each other, and a naive one cannot be"
            )
        object.__setattr__(self, "data", copied_mapping(self.data))

    @property
    def name(self) -> str:
        """The namespaced name a stream consumer subscribes to."""
        return f"{EVENT_NAMESPACE}.{self.type}"

    def to_dict(self) -> JSONObject:
        """The wire form, carrying the namespaced name."""
        return {
            "type": self.name,
            "agent": self.agent,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "node": self.node,
            "timestamp": self.timestamp.isoformat(),
            # Copied, so a consumer that redacts or normalizes what it was
            # handed cannot rewrite an event that has already been emitted.
            "data": copied_mapping(self.data),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ACPEvent":
        return cls(
            agent=as_str(data["agent"], field="agent"),
            type=as_str(data["type"], field="type"),
            session_id=as_optional_str(data.get("session_id"), field="session_id"),
            thread_id=as_optional_str(data.get("thread_id"), field="thread_id"),
            node=as_optional_str(data.get("node"), field="node"),
            timestamp=datetime.fromisoformat(
                as_str(data["timestamp"], field="timestamp")
            ),
            data=as_mapping(data.get("data"), field="data"),
        )


__all__ = ["ACPEvent", "ACPEventType", "EVENT_NAMESPACE"]
