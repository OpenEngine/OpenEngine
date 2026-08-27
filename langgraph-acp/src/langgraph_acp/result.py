"""What a node returns when its turn ends.

A string would lose the parts of a turn a workflow needs to act on: which
conversation it belonged to, why it stopped, and what it cost. `ACPResult`
keeps those, and a graph that only wants the text still reads `.message`.

`agent` and `session_id` are kept flat rather than wrapped in a type of their
own. Between them they name a conversation exactly, which is all a caller needs
to display it, record it, or hand it to a later invocation.

`content` and `tool_calls` stay JSON-shaped for now. Normalizing them into typed
blocks is a later ticket's work, and inventing the types early would mean
guessing at a shape before any agent has been read.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields

from langgraph_acp._json import (
    JSONObject,
    JSONValue,
    as_mapping,
    as_optional_float,
    as_optional_int,
    as_optional_str,
    as_sequence,
    as_str,
    copied_mapping,
    copied_sequence,
)


@dataclass(frozen=True, slots=True)
class ACPUsage:
    """What a turn or a session consumed.

    Every field defaults to `None`, and that is the point: "the agent did not
    report this" and "the agent reported zero" are different facts, and an
    aggregate built over invented zeros is wrong in a way nobody notices. A
    caller summing costs across a pull request must be able to see the gap.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    """Reasoning tokens, where the agent reports them separately."""
    cached_tokens: int | None = None
    """Input tokens served from cache, where the agent distinguishes them."""
    context_used: int | None = None
    """How much of the context window the conversation currently occupies."""
    context_size: int | None = None
    """The size of that window."""
    cost_usd: float | None = None
    """Cost as reported by the agent. Named for its unit; nothing converts."""

    def to_dict(self) -> JSONObject:
        """The reported fields only -- an absent key means "not reported"."""
        return {
            f.name: value
            for f in fields(self)
            if (value := getattr(self, f.name)) is not None
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ACPUsage":
        def count(name: str) -> int | None:
            return as_optional_int(data.get(name), field=name)

        return cls(
            input_tokens=count("input_tokens"),
            output_tokens=count("output_tokens"),
            thought_tokens=count("thought_tokens"),
            cached_tokens=count("cached_tokens"),
            context_used=count("context_used"),
            context_size=count("context_size"),
            cost_usd=as_optional_float(data.get("cost_usd"), field="cost_usd"),
        )


@dataclass(frozen=True, slots=True)
class ACPResult:
    """The normalized outcome of one ACP turn."""

    message: str = ""
    """The agent's final text, flattened for the common case."""
    content: Sequence[JSONValue] = ()
    """Final content blocks, with adjacent streamed text deltas coalesced."""
    agent: str | None = None
    """The registered name of the agent that ran the turn."""
    session_id: str | None = None
    """The conversation this turn ran in, once one exists."""
    stop_reason: str | None = None
    """Why the turn ended: `end_turn`, `cancelled`, `max_tokens`, ..."""
    usage: ACPUsage = ACPUsage()
    """Always present, though every field in it may be unreported."""
    tool_calls: Sequence[JSONValue] = ()
    """Tool activity from the turn, as the agent reported it."""
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    """Anything an adapter wants to carry that has no field of its own."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "content", copied_sequence(self.content, field="content")
        )
        object.__setattr__(
            self, "tool_calls", copied_sequence(self.tool_calls, field="tool_calls")
        )
        object.__setattr__(self, "metadata", copied_mapping(self.metadata))

    def to_dict(self) -> JSONObject:
        """A JSON-shaped view, suitable for LangGraph state or a checkpoint.

        Copied, nested containers included: a caller that normalizes the dict it
        was handed is not editing a result that has already been returned.
        """
        return {
            "message": self.message,
            "content": list(copied_sequence(self.content, field="content")),
            "agent": self.agent,
            "session_id": self.session_id,
            "stop_reason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "tool_calls": list(copied_sequence(self.tool_calls, field="tool_calls")),
            "metadata": copied_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ACPResult":
        return cls(
            message=as_str(data.get("message", ""), field="message"),
            content=as_sequence(data.get("content"), field="content"),
            agent=as_optional_str(data.get("agent"), field="agent"),
            session_id=as_optional_str(data.get("session_id"), field="session_id"),
            stop_reason=as_optional_str(data.get("stop_reason"), field="stop_reason"),
            usage=ACPUsage.from_dict(as_mapping(data.get("usage"), field="usage")),
            tool_calls=as_sequence(data.get("tool_calls"), field="tool_calls"),
            metadata=as_mapping(data.get("metadata"), field="metadata"),
        )


__all__ = ["ACPResult", "ACPUsage"]
