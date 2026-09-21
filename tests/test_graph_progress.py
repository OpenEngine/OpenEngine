"""The polling frontier follows events without reading checkpoint values."""

from engine.apps.web.graph_progress import GraphProgress
from engine.domain import RunId
from engine.graph_runtime import GraphId, NodeId, RunSnapshot, RunStatus
from engine.graph_runtime.events import EventKind, RuntimeEvent
from engine.graph_runtime.identity import ExecutionId
from engine.graph_runtime.topology import GraphEdge, GraphNode, GraphTopology


def test_frontier_tracks_parallel_executions_approvals_checkpoints_and_restarts():
    topology = GraphTopology(
        GraphId("graph"), "Graph", NodeId("review"),
        nodes=(GraphNode(NodeId("review"), "Review"), GraphNode(NodeId("join"), "Join")),
        edges=(GraphEdge(NodeId("review"), NodeId("join")),),
    )
    progress = GraphProgress(RunSnapshot(
        RunId("run"), topology.graph_id, RunStatus.RUNNING,
        next_nodes=(NodeId("review"),),
    ), topology)

    def emit(kind, execution=None, **payload):
        progress.apply(RuntimeEvent(
            RunId("run"), kind, payload, NodeId("review"),
            ExecutionId(execution) if execution else None,
        ))

    assert progress.json()["nextNodeIds"] == ["review"]
    emit(EventKind.NODE_STARTED, "one")
    emit(EventKind.NODE_STARTED, "two")
    emit(EventKind.APPROVAL_REQUESTED, "one", approvalId="a")
    emit(EventKind.APPROVAL_REQUESTED, "two", approvalId="b", autoApproved=True)
    emit(EventKind.APPROVAL_RESOLVED, "two", approvalId="b")
    assert progress.json() == {
        "activeNodeIds": ["review"], "waitingNodeIds": ["review"], "nextNodeIds": ["join"],
    }
    emit(EventKind.NODE_FINISHED, "two")
    assert progress.json()["activeNodeIds"] == ["review"]
    emit(EventKind.APPROVAL_RESOLVED, "one", approvalId="a")
    emit(EventKind.NODE_FINISHED, "one")
    assert progress.json()["activeNodeIds"] == []
    assert progress.json()["waitingNodeIds"] == []
    emit(EventKind.CHECKPOINT, nextNodes=["actual-branch"])
    assert progress.json()["nextNodeIds"] == ["actual-branch"]
    emit(EventKind.RUN_FAILED)
    assert all(value == [] for value in progress.json().values())
    emit(EventKind.RUN_FORKED, nodes=["review"])
    assert progress.json()["nextNodeIds"] == ["review"]
    emit(EventKind.NODE_STARTED, "three")
    emit(EventKind.APPROVAL_REQUESTED, "three", approvalId="c")
    emit(EventKind.RUN_FORKED, nodes=["review"])
    assert progress.json()["waitingNodeIds"] == []
    assert progress.json()["activeNodeIds"] == []
    emit(EventKind.RUN_FINISHED)
    assert all(value == [] for value in progress.json().values())
