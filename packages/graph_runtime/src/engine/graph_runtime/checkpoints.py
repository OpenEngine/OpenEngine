"""Where a run can be resumed from, and what it was holding when it got there.

A node is not a position. LangGraph saves a checkpoint per superstep, and time
travel means selecting an earlier checkpoint and executing forward from it;
`update_state` forks rather than rewriting what came before. So a checkpoint is
what this package addresses too, and "send it back to implementation" is a
*selector* that resolves to one -- see `engine.graph_runtime.api`.

The distinction is not pedantic. Given

    implementation -> review -> implementation -> review

there are two checkpoints from which `implementation` runs, and "go back to
implementation" has no single answer until one of them is named. Fan-out makes
it worse: a superstep is plural, so a position is a frontier of nodes rather
than a node.

History is a tree, not a list, and a resume appends to it:

    attempt 1  implementation -> review (rejected)
                     |
    attempt 2        `-- implementation -> review (accepted)

Both attempts stay, joined by `parent_id`. A workflow whose product is evidence
cannot pretend the first attempt never happened, and a rewind that truncated
would leave a run whose history contradicts what its subscribers were already
told.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NewType

from engine.graph_runtime.topology import NodeId

CheckpointId = NewType("CheckpointId", str)
"""One saved position in one run. Stable, and never reused by a fork."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One superstep boundary: what would run next, and the state it starts from."""

    checkpoint_id: CheckpointId
    parent_id: CheckpointId | None = None
    """Where this checkpoint came from. `None` only for a run's first."""
    next_nodes: tuple[NodeId, ...] = ()
    """The superstep this checkpoint executes forward into.

    Plural because a superstep is: three reviewers running together are one
    step, not three. Empty means the run had nowhere left to go.
    """
    values: Mapping[str, object] = field(default_factory=dict)
    """The graph's state at this boundary, which a resume restores."""
    source: str = "superstep"
    """Why it exists: "start", "superstep" as the run advanced, or "fork"."""


__all__ = ["Checkpoint", "CheckpointId"]
