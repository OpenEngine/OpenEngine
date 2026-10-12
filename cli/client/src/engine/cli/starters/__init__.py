"""Graphs `engine onboard` registers, so there is something to run on day one.

    review                  review your branch from four angles; changes nothing
    adversarial-review      attack a branch, then try to refute every finding; changes nothing
    implement-review        implement, review with a different agent, fix once
    spec-implement-review   spec it first, then the same

Plain YAML graphs (see cli/GRAPH_LANGUAGE.md), shipped as files so they read
the same here as anywhere else a graph is written. `engine graph run` also
registers one the first time it is named in a project, so these run out of
the box.
"""

from __future__ import annotations

from importlib.resources import files

import yaml

NAMES = ("review", "adversarial-review", "implement-review", "spec-implement-review")


def source(name: str, *, runner: str = "") -> str:
    """A starter graph's YAML; `runner` changes which runner it starts with."""
    if name not in NAMES:
        raise KeyError(f"no starter graph named {name!r}; choose from {', '.join(NAMES)}")
    text = files(__package__).joinpath(f"{name}.yaml").read_text(encoding="utf-8")  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
    if runner:
        text = text.replace(
            "    default: claude\n    choices: [claude, codex, least-utilized]",
            f"    default: {runner}\n    choices: [claude, codex, least-utilized]",
        )
    return text


def description(name: str) -> str:
    return str(yaml.safe_load(source(name))["description"])


__all__ = ["NAMES", "description", "source"]
