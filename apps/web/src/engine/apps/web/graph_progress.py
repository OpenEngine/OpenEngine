"""Small, process-local run frontiers maintained by graph lifecycle events."""

from engine.graph_runtime import RunSnapshot
from engine.graph_runtime.events import EventKind, RuntimeEvent
from engine.graph_runtime.topology import GraphTopology


class GraphProgress:
    def __init__(self, snapshot: RunSnapshot, topology: GraphTopology) -> None:
        self.topology = topology
        self.active = {
            str(one.execution_id): str(one.node_id)
            for one in snapshot.active_executions
        }
        self.waiting = {
            str(one.approval_id): str(one.node_id)
            for one in snapshot.pending_approvals
        }
        self.next_nodes = [str(node) for node in snapshot.next_nodes]

    def apply(self, event: RuntimeEvent) -> None:
        kind = event.kind
        if kind in (EventKind.RUN_FINISHED, EventKind.RUN_FAILED, EventKind.RUN_FORKED):
            self.active.clear()
            self.waiting.clear()
            self.next_nodes = (
                list(event.payload.get("nodes", ()))
                if kind is EventKind.RUN_FORKED else []
            )
        elif kind is EventKind.CHECKPOINT:
            self.active.clear()
            self.next_nodes = list(event.payload.get("nextNodes", ()))
        elif kind is EventKind.NODE_STARTED and event.node_id is not None:
            # Match snapshots: while running, approximate successors from the
            # topology; the next checkpoint supplies the actual routed frontier.
            self.active[str(event.execution_id or event.node_id)] = str(event.node_id)
            self.next_nodes = list(dict.fromkeys(
                str(edge.target) for edge in self.topology.edges
                if str(edge.source) in self.active.values()
            ))
        elif kind is EventKind.NODE_FINISHED:
            self.active.pop(str(event.execution_id or event.node_id), None)
        elif kind is EventKind.APPROVAL_REQUESTED and event.node_id is not None:
            self.waiting[str(event.payload["approvalId"])] = str(event.node_id)
        elif kind is EventKind.APPROVAL_RESOLVED:
            self.waiting.pop(str(event.payload["approvalId"]), None)

    def json(self) -> dict[str, list[str]]:
        return {
            "activeNodeIds": list(dict.fromkeys(self.active.values())),
            "waitingNodeIds": list(dict.fromkeys(self.waiting.values())),
            "nextNodeIds": list(self.next_nodes),
        }
