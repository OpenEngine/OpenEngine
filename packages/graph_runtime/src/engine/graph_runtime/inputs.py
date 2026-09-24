"""Declared string inputs shared by graph definitions and creation surfaces."""

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass

#: Runner choices that stand for a policy rather than a runner, and are replaced
#: by a concrete runner from the same input's choices when a run starts.
LEAST_UTILIZED = "least-utilized"
ROUND_ROBIN = "round-robin"
RUNNER_POLICIES = (LEAST_UTILIZED, ROUND_ROBIN)


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


def choose_runners(
    declarations: tuple[WorkflowInput, ...],
    inputs: Mapping[str, str],
    *,
    usage: Callable[[], Mapping[str, float]],
    turns: MutableMapping[str, int],
) -> dict[str, str]:
    """Replace runner policies with the runner each one picks for this run.

    `usage` answers each runner's highest reported used percentage, and is only
    asked when a least-utilized choice needs it; a runner with no reading counts
    as fully used, so a known-idle runner wins over an unknown one. With no
    reading for any of the input's runners there is nothing to compare, so the
    choice rotates as round-robin would rather than always landing on the
    first. `turns` is the round-robin position per input, advanced in place.
    """
    chosen = dict(inputs)
    for item in declarations:
        policy = inputs.get(item.name)
        runners = [choice for choice in item.choices if choice not in RUNNER_POLICIES]
        if policy not in RUNNER_POLICIES or not runners:
            continue
        used = usage() if policy == LEAST_UTILIZED else {}
        if any(runner in used for runner in runners):
            chosen[item.name] = min(runners, key=lambda runner: used.get(runner, 100.0))
        else:
            turn = turns.get(item.name, 0)
            turns[item.name] = turn + 1
            chosen[item.name] = runners[turn % len(runners)]
    return chosen
