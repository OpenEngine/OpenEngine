"""Whether a WorkOrder may reach the forge it came from.

A *connected* run does its work in the open: it pushes a branch, opens a pull
request, waits on CI and posts its review as comments. A *disconnected* run
does the same work in its own checkout and reaches nothing outside it -- no
push, no pull request, no comment -- and what it would have posted is kept in
the run for a person to read where they are watching it.

Chosen per run, at creation, through the workflow input named `MODE_INPUT`, so
one workflow serves both. Everything that behaves differently asks
`forge_mode` rather than reading the input itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class ForgeMode(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


#: The creation input a run's mode is chosen by.
MODE_INPUT = "mode"


def forge_mode(inputs: object) -> ForgeMode:
    """The mode a run's inputs chose. Connected unless they say otherwise."""
    if isinstance(inputs, Mapping) and inputs.get(MODE_INPUT) == ForgeMode.DISCONNECTED:
        return ForgeMode.DISCONNECTED
    return ForgeMode.CONNECTED


__all__ = ["ForgeMode", "MODE_INPUT", "forge_mode"]
