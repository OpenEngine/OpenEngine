/** The graph control surface, as the client reads it.
 *
 *  A second module beside `api.ts` rather than more of it: this is a different
 *  server with a different vocabulary -- graphs, runs, checkpoints, executions
 *  -- served under `/graph` so that its `/api/runs` and the interface's own are
 *  two URLs rather than one endpoint guessing which product it is answering
 *  for. Nothing here is reached unless `/api/config` said `graphRuntime`.
 *
 *  Whole snapshots, never diffs, for the reason the approval API takes the same
 *  shape: a browser that arrives mid-run has nothing to apply a diff to. */

import { api, type ApprovalDecision } from "./api";

/** Where the control surface is mounted. Matches `engine.apps.web.graphs`. */
export const GRAPH_PREFIX = "/graph";

export type GraphNode = {
  nodeId: string;
  /** What to call it on screen; the id when the graph named nothing better. */
  name: string;
  /** `agent`, `human`, `workspace`, or `node` for one that says nothing. */
  kind: string;
  description: string;
};

export type GraphEdge = {
  source: string;
  target: string;
  condition: string;
};

export type GraphTopology = {
  graphId: string;
  name: string;
  entryPoint: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
};

export type GraphRunStatus =
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed";

export type GraphExecution = { executionId: string; nodeId: string };

export type GraphApproval = {
  approvalId: string;
  executionId: string;
  nodeId: string;
  kind: string;
  reason: string;
  command: string;
  toolName: string;
  allowedDecisions: ApprovalDecision[];
};

export type GraphRun = {
  runId: string;
  graphId: string;
  status: GraphRunStatus;
  activeExecutions: GraphExecution[];
  /** What the run would execute next, which is a frontier and not a node. */
  nextNodes: string[];
  checkpointId: string | null;
  values: Record<string, unknown>;
  pendingApprovals: GraphApproval[];
  error: string;
};

/** Every kind of thing the feed carries. Unknown names are kept rather than
 *  dropped: a server that grew an event should not blank a page that has not
 *  learned it yet. */
export type GraphEvent = {
  sequence: number;
  type: string;
  runId: string;
  nodeId: string | null;
  executionId: string | null;
  payload: Record<string, unknown>;
};

export function listGraphs(signal?: AbortSignal): Promise<{ graphs: GraphTopology[] }> {
  return api<{ graphs: GraphTopology[] }>(`${GRAPH_PREFIX}/api/graphs`, { signal });
}

export function getGraphRun(runId: string, signal?: AbortSignal): Promise<GraphRun> {
  return api<GraphRun>(`${GRAPH_PREFIX}/api/runs/${encodeURIComponent(runId)}`, {
    signal,
  });
}

export function startGraphRun(
  graphId: string,
  values: Record<string, unknown>,
): Promise<GraphRun> {
  return api<GraphRun>(`${GRAPH_PREFIX}/api/runs`, {
    method: "POST",
    body: JSON.stringify({ graphId, values }),
  });
}

/** Answer what an execution stopped to ask.
 *
 *  Its own request rather than a reply on the feed that showed it: the
 *  connection that presented the pause may be long gone, and the answer has to
 *  reach the execution either way. */
export function decideGraphApproval(
  runId: string,
  approvalId: string,
  decision: ApprovalDecision,
): Promise<GraphRun> {
  return api<GraphRun>(
    `${GRAPH_PREFIX}/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: "POST", body: JSON.stringify({ decision }) },
  );
}

/** Say something to an execution that is already running.
 *
 *  Not a message about the graph: it reaches the agent session mid-turn, which
 *  is why a decision note can be sent this way without the node being
 *  restarted. */
export function steerGraphRun(
  runId: string,
  message: string,
  target?: { node?: string; execution?: string },
): Promise<GraphRun> {
  return api<GraphRun>(
    `${GRAPH_PREFIX}/api/runs/${encodeURIComponent(runId)}/steering`,
    { method: "POST", body: JSON.stringify({ message, ...target }) },
  );
}

/** Send a run back to a node, forking rather than rewriting. */
export function transitionGraphRun(runId: string, node: string): Promise<GraphRun> {
  return api<GraphRun>(
    `${GRAPH_PREFIX}/api/runs/${encodeURIComponent(runId)}/transitions`,
    { method: "POST", body: JSON.stringify({ node }) },
  );
}

export function graphRunEventsUrl(runId: string): string {
  return `${GRAPH_PREFIX}/api/runs/${encodeURIComponent(runId)}/events`;
}

/** Which of a graph's nodes a run has finished, is in, or is waiting on.
 *
 *  Derived rather than reported, and deliberately: the contract has no visited
 *  list, because a line of nodes reads well for a pipeline and lies about
 *  fan-out, loops and retries. What a *pipeline* looks like on screen is this
 *  function's business and nobody else's. */
export type NodeStatus =
  | "pending"
  | "in_progress"
  | "action_required"
  | "completed"
  | "failed";

export function nodeStatuses(
  topology: GraphTopology | undefined,
  run: GraphRun | undefined,
  finished: ReadonlySet<string>,
): Map<string, NodeStatus> {
  const statuses = new Map<string, NodeStatus>();
  if (!topology) return statuses;
  const active = new Set((run?.activeExecutions ?? []).map((one) => one.nodeId));
  const waiting = new Set((run?.pendingApprovals ?? []).map((one) => one.nodeId));
  for (const node of topology.nodes) {
    const done = finished.has(node.nodeId) || run?.status === "completed";
    statuses.set(
      node.nodeId,
      waiting.has(node.nodeId)
        ? "action_required"
        : active.has(node.nodeId)
          ? "in_progress"
          : done
            ? "completed"
            : run?.status === "failed" && active.has(node.nodeId)
              ? "failed"
              : "pending",
    );
  }
  return statuses;
}

/** The one word the page's chip shows.
 *
 *  A finished run reads as its outcome; a moving one reads as the stage doing
 *  the work, which is what an operator is actually watching. */
export function graphRunLabel(
  topology: GraphTopology | undefined,
  run: GraphRun | undefined,
): string {
  if (!run) return "…";
  if (run.status === "completed") return "succeeded";
  if (run.status === "failed") return "failed";
  const here =
    run.pendingApprovals[0]?.nodeId ??
    run.activeExecutions[0]?.nodeId ??
    run.nextNodes[0];
  const named = topology?.nodes.find((node) => node.nodeId === here);
  return named?.name ?? here ?? run.status;
}
