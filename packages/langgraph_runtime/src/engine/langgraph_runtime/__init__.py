"""An API control surface over a LangGraph workflow.

Six capabilities, stated once as a protocol and served once over HTTP:

* start runs
* inspect the current state of one
* describe a graph's topology
* subscribe to what a run raises -- approval requests and transcript events,
  tool calls included
* steer: deliver a message to a node that is already running, and have it
  continue from where it was interrupted
* transition by hand: send a run back to an earlier node, such as
  implementation, with the state it had when that node was entered

`GraphRuntime` is the contract; `create_app` is the surface. LangGraph is the
intended implementation and is not named anywhere in this package: the server is
built and tested against the contract first, so the binding arrives with its
tests already written.
"""

from engine.langgraph_runtime.api import create_app
from engine.langgraph_runtime.control import (
    ApprovalNotPendingError,
    GraphRuntime,
    LangGraphRuntimeError,
    PendingApproval,
    RunNotSteerableError,
    RunSnapshot,
    RunStatus,
    UnknownApprovalError,
    UnknownGraphError,
    UnknownNodeError,
    UnknownRunError,
)
from engine.langgraph_runtime.events import (
    EventKind,
    EventLog,
    EventObserver,
    RuntimeEvent,
)
from engine.langgraph_runtime.topology import (
    GraphEdge,
    GraphId,
    GraphNode,
    GraphTopology,
    NodeId,
)

__all__ = [
    "ApprovalNotPendingError",
    "EventKind",
    "EventLog",
    "EventObserver",
    "GraphEdge",
    "GraphId",
    "GraphNode",
    "GraphRuntime",
    "GraphTopology",
    "LangGraphRuntimeError",
    "NodeId",
    "PendingApproval",
    "RunNotSteerableError",
    "RunSnapshot",
    "RunStatus",
    "RuntimeEvent",
    "UnknownApprovalError",
    "UnknownGraphError",
    "UnknownNodeError",
    "UnknownRunError",
    "create_app",
]
