"""The stages a WorkOrder moves through, named once for every layer.

A workflow's nodes are many and its own business; the states they belong to
are few and shared. A node declares its state, the topology carries it to
clients as the node's group, and anything that wants to say "the review" --
a WorkOrder page, the terminal workbench, a findings view -- asks for the
state rather than spelling the word itself.

A `StrEnum`, so the value *is* the wire spelling: a node whose group is
`WorkState.REVIEW` is described to a client as `"Review"`, the same string a
node declaring it by hand always produced.

A run may also *start* in a later state than the first. The creation input
named `STATE_INPUT` says which, so a workflow that already knows how to review
its own work can be pointed at somebody else's change instead: started in
`WorkState.REVIEW`, it skips deciding and doing and reviews what it is given.
Everything that behaves differently asks `start_state` rather than reading the
input itself, the way `engine.domain.forge` does for a run's mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class WorkState(StrEnum):
    """Where a WorkOrder is: deciding what to do, doing it, or checking it."""

    PLANNING = "Planning"
    IMPLEMENTATION = "Implementation"
    REVIEW = "Review"


#: The creation input a run's first state is chosen by.
STATE_INPUT = "state"


def start_state(inputs: object) -> WorkState:
    """The state a run's inputs start it in. Planning unless they say otherwise."""
    if isinstance(inputs, Mapping):
        try:
            return WorkState(inputs.get(STATE_INPUT) or WorkState.PLANNING)
        except ValueError:
            pass
    return WorkState.PLANNING


__all__ = ["STATE_INPUT", "WorkState", "start_state"]
