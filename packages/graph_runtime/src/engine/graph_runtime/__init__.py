"""An API control surface over a workflow graph.

Six capabilities, stated once as a protocol and served once over HTTP:

* start runs
* inspect the current state of one
* describe a graph's topology
* subscribe to what a run raises -- approval requests and transcript events,
  tool calls included
* steer: deliver a message to an execution that is already running, without
  interrupting or restarting the graph node driving it
* send a run back to an earlier position, forking rather than rewriting

`GraphRuntime` is the contract; `create_app` is the surface. LangGraph is the
intended implementation and is not named anywhere in this package, and neither
are ACP, Claude or Codex: the server is built and tested against the contract
first, so a binding arrives with its tests already written.

Three shapes here are deliberate, and each of them is a place a simpler design
would have had to be undone later:

**A position is a frontier of executions, not a node.** A superstep may run
several nodes at once -- and several tasks into the *same* node, which is what
`Send` does -- so `RunSnapshot` reports `active_executions` and `next_nodes` and
never claims a single current node. Every in-flight thing has an `ExecutionId`,
and control is addressed to that rather than to a node name. Nor is there a
visited list: a line of nodes reads well for a pipeline and lies about fan-out,
loops and retries. What happened is `history()` and the feed.

**A resume names a checkpoint, not a node.** See
`engine.graph_runtime.checkpoints`: a graph with a loop has been about to run
`implementation` more than once, so "send it back to implementation" is a
selector the HTTP layer resolves, and the primitive underneath is
`resume_from`. Forks append; nothing is truncated, so an abandoned attempt stays
readable beside the one that replaced it.

**Steering and approvals go to the execution, not through the graph.** See
`engine.graph_runtime.executions` for the boundary between the human-in-the-loop
questions a session answers and the ones a graph interrupt answers.
"""

from engine.graph_runtime.api import create_app
from engine.graph_runtime.checkpoints import Checkpoint, CheckpointId
from engine.graph_runtime.control import (
    AmbiguousExecutionError,
    ApprovalNotPendingError,
    GraphRuntime,
    GraphRuntimeError,
    NoSuchPositionError,
    PendingApproval,
    RunNotSteerableError,
    RunSnapshot,
    RunStatus,
    UnknownApprovalError,
    UnknownCheckpointError,
    UnknownGraphError,
    UnknownNodeError,
    UnknownRunError,
)
from engine.graph_runtime.events import (
    EventKind,
    EventLog,
    EventObserver,
    RuntimeEvent,
)
from engine.graph_runtime.executions import ControllableExecution, ExecutionRegistry
from engine.graph_runtime.identity import ActiveExecution, ExecutionId
from engine.graph_runtime.topology import (
    GraphEdge,
    GraphId,
    GraphNode,
    GraphTopology,
    NodeId,
)

__all__ = [
    "ActiveExecution",
    "AmbiguousExecutionError",
    "ApprovalNotPendingError",
    "Checkpoint",
    "CheckpointId",
    "ControllableExecution",
    "EventKind",
    "EventLog",
    "EventObserver",
    "ExecutionId",
    "ExecutionRegistry",
    "GraphEdge",
    "GraphId",
    "GraphNode",
    "GraphRuntime",
    "GraphRuntimeError",
    "GraphTopology",
    "NoSuchPositionError",
    "NodeId",
    "PendingApproval",
    "RunNotSteerableError",
    "RunSnapshot",
    "RunStatus",
    "RuntimeEvent",
    "UnknownApprovalError",
    "UnknownCheckpointError",
    "UnknownGraphError",
    "UnknownNodeError",
    "UnknownRunError",
    "create_app",
]
