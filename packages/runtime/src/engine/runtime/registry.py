"""Late-bound adapter resolution.

The composition root has to name a concrete implementation *somewhere*. The
question is whose composition root. For a deployable we run ourselves, an import
is fine. For a surface we ship to consumers, an import is a weld: swapping Claude
for Strands would mean editing our code rather than their configuration.

So provider selection happens by **name**, resolved from packaging entry points
at startup:

    [project.entry-points."engine.agent_runners"]
    claude = "engine.adapters.anthropic:build_agent_runner"

A consumer installs whichever adapter packages they want, sets
`ENGINE_AGENT_RUNNER`, and never edits ours. An adapter we have never heard of
works exactly as well as one we shipped.

> This is the one deliberate hole in the static boundary checks. `tests/` proves
> by AST that no core package *imports* an adapter -- and this module doesn't, so
> it passes. But it does *load* one at runtime. That is the intended seam rather
> than a leak, and `test_registry_names_no_adapter` pins it: the resolution is
> data-driven, and this file names no vendor.
"""

from collections.abc import Iterable, Sequence
from importlib.metadata import EntryPoint, entry_points
from typing import Any

#: Entry-point group an agent-runner adapter registers itself under.
AGENT_RUNNER_GROUP = "engine.agent_runners"


class RunnerUnavailable(RuntimeError):
    """An adapter is installed but cannot run right now.

    Raised by a factory when its prerequisites are missing -- no credentials, no
    binary on PATH, no reachable host. Distinct from "not installed" so a caller
    can fall through a preference list without swallowing real errors.
    """


class UnknownRunner(LookupError):
    """No installed package registered an agent runner under that name."""

    def __init__(self, name: str, known: Iterable[str]) -> None:
        options = ", ".join(sorted(known)) or "none installed"
        super().__init__(f"no agent runner named {name!r}; available: {options}")
        self.name = name


def available_agent_runners() -> dict[str, EntryPoint]:
    """Every agent runner any installed package registered."""
    return {ep.name: ep for ep in entry_points(group=AGENT_RUNNER_GROUP)}


def load_agent_runner(name: str, **options: Any) -> Any:
    """Build the named runner. Raises UnknownRunner or RunnerUnavailable."""
    found = available_agent_runners()
    if name not in found:
        raise UnknownRunner(name, found)
    factory = found[name].load()
    return factory(**options)


def resolve_agent_runner(
    preference: Sequence[str], **options: Any
) -> tuple[Any, str]:
    """Try each name in order; return the first that builds, and its name.

    Names that are not installed, or whose prerequisites are missing, are skipped
    -- that is what makes "live if credentials exist, demo otherwise" a
    configuration decision rather than an `if` statement in an app.
    """
    tried: list[str] = []
    for name in preference:
        try:
            return load_agent_runner(name, **options), name
        except (UnknownRunner, RunnerUnavailable) as error:
            tried.append(f"{name} ({error})")
    raise RunnerUnavailable(
        "no usable agent runner. Tried: " + "; ".join(tried or ["nothing"])
    )


__all__ = [
    "AGENT_RUNNER_GROUP",
    "RunnerUnavailable",
    "UnknownRunner",
    "available_agent_runners",
    "load_agent_runner",
    "resolve_agent_runner",
]
