"""What a node asks of an agent: session settings, and the capabilities it needs.

Two different questions, deliberately separated.

`ACPConfig` is a request about *this* conversation -- model, mode, thought
level. It is expressed as mappings rather than one keyword argument per option
because the set of options is the agent's to define, and a library that names
them all in its own signature is a library that must be released again whenever
an agent adds one.

`ACPRequirements` is a statement about the agent itself. It exists so a
mismatch surfaces during capability negotiation, with a name attached, rather
than as a prompt that quietly does less than the workflow assumed.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum

from langgraph_acp._json import JSONValue, copied_mapping


class UnsupportedOption(StrEnum):
    """What to do with a requested setting the agent does not offer."""

    ERROR = "error"
    """Refuse to run. The default: a silently dropped setting is a wrong answer."""

    WARN = "warn"
    """Log the omission and continue."""

    IGNORE = "ignore"
    """Continue without comment."""


@dataclass(frozen=True, slots=True)
class ACPConfig:
    """Session settings, requested semantically where possible.

    `by_category` names a meaning that agents agree on -- "model", "mode",
    "thought_level" -- and leaves it to the adapter to find the matching option
    the agent advertises. `by_id` addresses one agent's option by its exact
    identifier, which is precise and correspondingly unportable.
    """

    by_category: Mapping[str, JSONValue] = field(default_factory=dict)
    """Requested settings, keyed by semantic category."""
    by_id: Mapping[str, JSONValue] = field(default_factory=dict)
    """Requested settings, keyed by an agent's own option id."""
    unsupported: UnsupportedOption | str = UnsupportedOption.ERROR
    """How to treat a requested setting the agent does not advertise."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_category", copied_mapping(self.by_category))
        object.__setattr__(self, "by_id", copied_mapping(self.by_id))
        object.__setattr__(self, "unsupported", UnsupportedOption(self.unsupported))


@dataclass(frozen=True, slots=True)
class ACPRequirements:
    """Capabilities the workflow cannot do without.

    Each flag is a requirement, not a preference: `False` means "this workflow
    does not care", never "the agent must not support it".
    """

    resume: bool = False
    """The agent must be able to continue an earlier session."""
    mcp: bool = False
    """The agent must accept MCP servers supplied at session creation."""
    elicitation: bool = False
    """The agent must be able to ask the caller for structured input."""

    @property
    def required(self) -> tuple[str, ...]:
        """The names of the capabilities being demanded, in declaration order.

        Read off the dataclass rather than a second hand-written list, so a
        capability added later cannot be added to only one of them.
        """
        return tuple(f.name for f in fields(self) if getattr(self, f.name))


__all__ = ["ACPConfig", "ACPRequirements", "UnsupportedOption"]
