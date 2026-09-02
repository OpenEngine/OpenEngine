"""Who is executing, as distinct from what they are executing.

A node name does not identify something in flight. LangGraph's `Send` can fan
several tasks into one node, so `review` may be three executions at once --
three sessions, three transcripts, three approvals to answer separately. Control
and events are addressed to the execution; the node is what a person is shown.

Its own module because both the event log and the contract need it, and they sit
either side of each other: `control` imports `events` for the observer type, so
an id declared in `control` could not travel on an event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

from engine.graph_runtime.topology import NodeId

ExecutionId = NewType("ExecutionId", str)
"""One thing in flight. Unique within a run, and never reused by a second pass."""


@dataclass(frozen=True, slots=True)
class ActiveExecution:
    """One in-flight execution, and the node it is an execution of.

    Both, because both are needed: the id is what a client addresses, and the
    node is what it can show a person. An answer that carried only the id would
    make a UI fetch the topology to render "reviewer-2 is working".
    """

    execution_id: ExecutionId
    node_id: NodeId


__all__ = ["ActiveExecution", "ExecutionId"]
