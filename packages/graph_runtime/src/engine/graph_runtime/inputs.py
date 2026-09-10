"""Declared string inputs shared by graph definitions and creation surfaces."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowInput:
    """A text field, or a dropdown when choices are supplied."""

    name: str
    label: str
    default: str = ""
    required: bool = False
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.label.strip():
            raise ValueError("workflow inputs need a name and label")
        if self.default and self.choices and self.default not in self.choices:
            raise ValueError(f"invalid default for workflow input {self.name!r}")


def resolve_inputs(
    declarations: tuple[WorkflowInput, ...], values: object
) -> dict[str, str]:
    """Apply defaults and reject invalid values before starting a run."""
    if not isinstance(values, Mapping):
        raise ValueError("inputs must be an object")
    unknown = set(values) - {item.name for item in declarations}
    if unknown:
        raise ValueError(f"unknown workflow inputs: {', '.join(sorted(unknown))}")
    resolved = {}
    for item in declarations:
        value = values.get(item.name, item.default)
        if not isinstance(value, str):
            raise ValueError(f"workflow input {item.name!r} must be a string")
        if item.required and not value.strip():
            raise ValueError(f"workflow input {item.name!r} is required")
        if value and item.choices and value not in item.choices:
            raise ValueError(f"invalid choice for workflow input {item.name!r}")
        resolved[item.name] = value
    return resolved
