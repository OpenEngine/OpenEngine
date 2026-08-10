"""Tool declarations: what an agent is allowed to call.

An `AgentProfile` grants tools by name; a `ToolSpec` is what that name resolves
to when a runner has to describe the tool to a model.

Parameters are a typed list rather than a raw JSON Schema document for two
reasons: a nested dict inside a frozen dataclass is mutable in all the ways this
layer avoids, and JSON Schema is a wire format, so building one belongs to the
adapter that speaks the wire. A tool needing richer structure than this can
declare an `OBJECT` parameter and own the parsing of its contents.
"""

from dataclasses import dataclass, field
from enum import Enum


class ToolParameterType(Enum):
    """The JSON types a parameter may take."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class ToolParameter:
    """One argument a tool accepts."""

    name: str
    type: ToolParameterType = ToolParameterType.STRING
    description: str = ""
    required: bool = True
    choices: tuple[str, ...] = field(default=())
    """If set, the only accepted values -- rendered as an enum."""
    item_type: ToolParameterType | None = None
    """Element type, when `type` is ARRAY."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as the model sees it: a name, a purpose, and a signature."""

    name: str
    description: str = ""
    parameters: tuple[ToolParameter, ...] = field(default=())

    @property
    def required_parameters(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters if p.required)


__all__ = ["ToolParameter", "ToolParameterType", "ToolSpec"]
