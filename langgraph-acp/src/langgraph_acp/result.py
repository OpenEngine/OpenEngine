"""What a node returns when its turn ends.

A string would lose the parts of a turn a workflow needs to act on: which
conversation it belonged to, why it stopped, and what it cost. `ACPResult`
keeps those, and a graph that only wants the text still reads `.message`.

`content` and `tool_calls` stay JSON-shaped for now. Normalizing them into typed
blocks is a later ticket's work, and inventing the types early would mean
guessing at a shape before any agent has been read.
"""

from dataclasses import dataclass, field, fields

from langgraph_acp._json import (
    JSONObject,
    JSONValue,
    copied_mapping,
    copied_sequence,
)
from langgraph_acp.session import ACPSessionRef


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
    def from_dict(cls, data: JSONObject) -> "ACPUsage":
        return cls(**{f.name: data.get(f.name) for f in fields(cls)})


@dataclass(frozen=True, slots=True)
class ACPResult:
    """The normalized outcome of one ACP turn."""

    message: str = ""
    """The agent's final text, flattened for the common case."""
    content: tuple[JSONValue, ...] = ()
    """The final content blocks, as the agent sent them."""
    session: ACPSessionRef | None = None
    """The conversation this turn ran in, once one exists."""
    stop_reason: str | None = None
    """Why the turn ended: `end_turn`, `cancelled`, `max_tokens`, ..."""
    usage: ACPUsage = ACPUsage()
    """Always present, though every field in it may be unreported."""
    tool_calls: tuple[JSONValue, ...] = ()
    """Tool activity from the turn, as the agent reported it."""
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    """Anything an adapter wants to carry that has no field of its own."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", copied_sequence(self.content))
        object.__setattr__(self, "tool_calls", copied_sequence(self.tool_calls))
        object.__setattr__(self, "metadata", copied_mapping(self.metadata))

    def to_dict(self) -> JSONObject:
        """A JSON-shaped view, suitable for LangGraph state or a checkpoint."""
        return {
            "message": self.message,
            "content": list(self.content),
            "session": self.session.to_dict() if self.session is not None else None,
            "stop_reason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "tool_calls": list(self.tool_calls),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: JSONObject) -> "ACPResult":
        session = data.get("session")
        return cls(
            message=str(data.get("message", "")),
            content=data.get("content") or (),
            session=ACPSessionRef.from_dict(session) if session else None,
            stop_reason=data.get("stop_reason"),
            usage=ACPUsage.from_dict(data.get("usage") or {}),
            tool_calls=data.get("tool_calls") or (),
            metadata=data.get("metadata") or {},
        )


__all__ = ["ACPResult", "ACPUsage"]
