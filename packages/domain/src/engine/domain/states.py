"""The stages a WorkOrder moves through, named once for every layer.

A workflow's nodes are many and its own business; the states they belong to
are few and shared. A node declares its state, the topology carries it to
clients as the node's group, and anything that wants to say "the review" --
a WorkOrder page, the terminal workbench, a findings view -- asks for the
state rather than spelling the word itself.

A `StrEnum`, so the value *is* the wire spelling: a node whose group is
`WorkState.REVIEW` is described to a client as `"Review"`, the same string a
node declaring it by hand always produced.
"""

from __future__ import annotations

from enum import StrEnum


class WorkState(StrEnum):
    """Where a WorkOrder is: deciding what to do, doing it, or checking it."""

    PLANNING = "Planning"
    IMPLEMENTATION = "Implementation"
    REVIEW = "Review"


__all__ = ["WorkState"]
