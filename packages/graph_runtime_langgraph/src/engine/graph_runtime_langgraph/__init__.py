"""`engine.graph_runtime`'s contract, implemented against LangGraph.

    engine.graph_runtime            what a graph must be able to do
        ^
        | implements
        |
    engine.graph_runtime_langgraph  LangGraph, plus langgraph-acp for agents

The mapping is deliberately thin, because LangGraph already has every primitive
the contract asks for:

    RunId          a thread            CheckpointId   a checkpoint
    ExecutionId    a task id           next_nodes     a checkpoint's `next`

Which leaves three things worth reading about, each in its own module:

* `runtime` -- how a run is started so that it has a position before it moves,
  and how a fork is written as a child of the attempt it replaces.
* `executions` -- why control is addressed to a LangGraph task id, and how a
  node finds its own.
* `acp` -- how an agent's request for permission is answered by a process that
  did not raise it, without starting the conversation over.

Nothing ACP-shaped reaches `engine.graph_runtime`. The generic runtime sees a
`ControllableExecution` with two methods; that it happens to be holding a Claude
or Codex session is this package's business and no layer above it needs to know.
"""

from engine.graph_runtime_langgraph.acp import (
    APPROVAL_ID,
    EXECUTION_ID,
    NODE_ID,
    RUN_ID,
    ACPNode,
    answer_permission,
)
from engine.graph_runtime_langgraph.executions import (
    NodeExecution,
    NoExecutionError,
    current_execution,
)
from engine.graph_runtime_langgraph.graphs import DescribesItself, LangGraphDefinition
from engine.graph_runtime_langgraph.runtime import LangGraphRuntime
from engine.graph_runtime_langgraph.store import (
    ApprovalRecord,
    GraphRuntimeStore,
    InMemoryGraphRuntimeStore,
    RunRecord,
    SqliteGraphRuntimeStore,
)

__all__ = [
    "APPROVAL_ID",
    "EXECUTION_ID",
    "NODE_ID",
    "RUN_ID",
    "ACPNode",
    "ApprovalRecord",
    "DescribesItself",
    "GraphRuntimeStore",
    "InMemoryGraphRuntimeStore",
    "LangGraphDefinition",
    "LangGraphRuntime",
    "NoExecutionError",
    "NodeExecution",
    "RunRecord",
    "SqliteGraphRuntimeStore",
    "answer_permission",
    "current_execution",
]
