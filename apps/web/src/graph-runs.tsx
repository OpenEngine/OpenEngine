/** The WorkOrder pages, driven by the graph control surface.
 *
 *  The same three screens `runs.tsx` draws -- start one, watch it, decide it --
 *  against `engine.graph_runtime` instead of the workflow API. Beside that file
 *  rather than inside it, because the two servers do not answer the same
 *  questions: this one reports a *frontier* of executions rather than a current
 *  step, has no run list, and takes a human decision as an approval. A single
 *  component branching on which backend it was talking to would be a translation
 *  layer pretending to be a page.
 *
 *  Which of them the shell mounts is `config.graphRuntime`, which on the server
 *  is whether any loaded workflow runs as a graph. If none does, nothing here is
 *  loaded. */

import { type FormEvent, useEffect, useMemo, useState } from "react";

import type { ApprovalDecision, EngineConfig } from "./api";
import { Stat, StatStrip } from "./brand";
import {
  decideGraphApproval,
  getGraphRun,
  graphRunEventsUrl,
  graphRunLabel,
  listGraphs,
  nodeStatuses,
  startGraphRun,
  steerGraphRun,
  type GraphApproval,
  type GraphEvent,
  type GraphRun,
  type GraphTopology,
} from "./graph-api";

/** Where the unsent task prompt waits between visits to the form, exactly as
 *  the workflow form keeps it: a prompt worth writing is worth several
 *  sittings. Its own key, so switching backends does not hand one page the
 *  other's draft. */
const DRAFT_KEY = "engine.graphRunDraft";

/** What `human-review` asks under. The one approval the page answers with a
 *  decision rather than with permission. */
const HUMAN_REVIEW_TOOL = "human_review";

export function useGraphTopologies() {
  const [graphs, setGraphs] = useState<GraphTopology[]>([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    listGraphs(controller.signal)
      .then((value) => {
        setGraphs(value.graphs);
        setLoaded(true);
      })
      .catch((reason: Error) => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, []);
  return { graphs, error, loaded };
}

/** The name a graph is offered under.
 *
 *  One graph per runner is how the runner is chosen -- `ACPNode` names its
 *  agent -- so the suffix on the id is the runner, and that is what the form
 *  labels its options with. */
export function runnerOf(graph: GraphTopology): string {
  const parts = graph.graphId.split("-");
  return parts[parts.length - 1] ?? graph.graphId;
}

export function GraphNewRunPage({ config }: { config: EngineConfig }) {
  const { graphs, error: graphsError, loaded } = useGraphTopologies();
  const [prompt, setPrompt] = useState(
    () => window.localStorage.getItem(DRAFT_KEY) ?? "",
  );
  const [repository, setRepository] = useState(".");
  const [graphId, setGraphId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (prompt) window.localStorage.setItem(DRAFT_KEY, prompt);
    else window.localStorage.removeItem(DRAFT_KEY);
  }, [prompt]);

  // Settled once the graphs arrive, and only then: the select cannot offer a
  // default before it knows what there is to default to.
  useEffect(() => {
    const preferred =
      graphs.find((graph) => runnerOf(graph) === config.defaultWorkflowRunner) ??
      graphs[0];
    if (preferred) setGraphId((current) => current || preferred.graphId);
  }, [graphs, config.defaultWorkflowRunner]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const run = await startGraphRun(graphId, { task: prompt, repository });
      window.localStorage.removeItem(DRAFT_KEY);
      window.location.assign(`/runs/${encodeURIComponent(run.runId)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create WorkOrder");
      setSubmitting(false);
    }
  }

  return (
    <main className="panel-scroll">
      <header className="hero hero-narrow">
        <p className="eyebrow">OpenEngine / New WorkOrder</p>
        <h1>Create a WorkOrder</h1>
        <p className="lede">
          One WorkOrder, run as a graph: a checkout, an implementation, a review, and
          the decision that ends it.
        </p>
      </header>
      <form className="form" onSubmit={submit}>
        <label>
          <span>Repository</span>
          <input
            required
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="owner/repository or local path"
          />
        </label>
        <label>
          <span>Implementation runner</span>
          <select
            required
            value={graphId}
            onChange={(event) => setGraphId(event.target.value)}
          >
            {graphs.map((graph) => (
              <option key={graph.graphId} value={graph.graphId}>
                {runnerOf(graph)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Task prompt</span>
          <textarea
            required
            rows={9}
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="Describe what the implementation agent should change and what success looks like."
          />
        </label>
        {(error || graphsError) && (
          <p className="notice" role="alert">
            {error || `Could not load graphs: ${graphsError}`}
          </p>
        )}
        <div className="form-actions">
          <a className="back-link" href="/runs">
            Cancel
          </a>
          <button
            className="btn btn-primary"
            disabled={submitting || !loaded || !graphId}
            type="submit"
          >
            {submitting ? "Creating…" : "Create WorkOrder"}
          </button>
        </div>
      </form>
    </main>
  );
}

/** Everything the run has raised, replayed from the beginning on every mount.
 *
 *  Server-sent, and identified, so a reconnecting browser is replayed from
 *  where it got to rather than told to poll. The snapshot beside it is polled
 *  because it is a whole answer: a page that only followed the feed would have
 *  to rebuild the run's position from its history. */
function useGraphRun(runId: string) {
  const [run, setRun] = useState<GraphRun>();
  const [events, setEvents] = useState<GraphEvent[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      getGraphRun(runId)
        .then((value) => {
          if (cancelled) return;
          setRun(value);
          setError("");
        })
        .catch((reason: Error) => {
          if (!cancelled) setError(reason.message);
        })
        .finally(() => {
          if (!cancelled) timer = window.setTimeout(load, 1000);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId]);

  useEffect(() => {
    setEvents([]);
    const source = new EventSource(graphRunEventsUrl(runId));
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as GraphEvent;
      // Keyed by sequence rather than appended blindly: the feed replays, and
      // a reconnect that re-delivered what was already on screen would show
      // every message twice.
      setEvents((current) =>
        current.some((seen) => seen.sequence === event.sequence)
          ? current
          : [...current, event].sort((a, b) => a.sequence - b.sequence),
      );
    };
    return () => source.close();
  }, [runId]);

  // Handed out so a decision can be applied at once rather than a second
  // later, when the poll next answers -- and the poll is still what corrects it
  // if the write did not take.
  return { run, events, error, setRun };
}

type NodeActivity = {
  transcript: { role: string; text: string }[];
  tools: { callId: string; name: string }[];
};

function activityByNode(events: readonly GraphEvent[]): Map<string, NodeActivity> {
  const activity = new Map<string, NodeActivity>();
  const of = (nodeId: string) => {
    const found = activity.get(nodeId) ?? { transcript: [], tools: [] };
    activity.set(nodeId, found);
    return found;
  };
  for (const event of events) {
    if (!event.nodeId) continue;
    if (event.type === "transcript")
      of(event.nodeId).transcript.push({
        role: String(event.payload.role ?? "assistant"),
        text: String(event.payload.text ?? ""),
      });
    else if (event.type === "tool.call")
      of(event.nodeId).tools.push({
        callId: String(event.payload.callId ?? ""),
        name: String(event.payload.name ?? ""),
      });
  }
  return activity;
}

function finishedNodes(events: readonly GraphEvent[]): Set<string> {
  const done = new Set<string>();
  for (const event of events) {
    if (event.type === "node.finished" && event.nodeId) done.add(event.nodeId);
    // A fork re-attempts a node, so what it finished before is no longer true
    // of the attempt on screen.
    if (event.type === "run.forked")
      for (const node of (event.payload.nodes as string[] | undefined) ?? [])
        done.delete(node);
  }
  return done;
}

/** The decision that ends a run, on the run it ends.
 *
 *  Two requests rather than one, because the surface underneath has two: the
 *  note is said to the execution that is waiting, and the decision answers what
 *  it asked. Sent in that order, so the note is already queued when the node
 *  wakes up to read it. */
function HumanReviewDecision({
  runId,
  approval,
  onDecided,
}: {
  runId: string;
  approval: GraphApproval;
  onDecided: (run: GraphRun) => void;
}) {
  const [note, setNote] = useState("");
  const [deciding, setDeciding] = useState<"approve" | "reject">();
  const [error, setError] = useState("");

  async function decide(approved: boolean) {
    setDeciding(approved ? "approve" : "reject");
    setError("");
    try {
      if (note.trim())
        await steerGraphRun(runId, note.trim(), { execution: approval.executionId });
      onDecided(
        await decideGraphApproval(
          runId,
          approval.approvalId,
          approved ? "accept" : "cancel",
        ),
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      setDeciding(undefined);
    }
  }

  return (
    <div className="decision">
      <label>
        <span>Decision note</span>
        <textarea
          rows={3}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="Optional — why this WorkOrder was approved or rejected."
        />
      </label>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      <div className="decision-actions">
        <button
          type="button"
          className="btn btn-primary"
          disabled={deciding !== undefined}
          onClick={() => void decide(true)}
        >
          {deciding === "approve" ? "Approving…" : "Approve"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={deciding !== undefined}
          onClick={() => void decide(false)}
        >
          {deciding === "reject" ? "Rejecting…" : "Reject"}
        </button>
      </div>
    </div>
  );
}

/** Permission for one thing an agent wants to do, beside what it wants to do.
 *
 *  Every decision the request permits, and no more: what a provider can honour
 *  is the provider's to say, and offering an answer it would refuse would be
 *  the interface making a promise on somebody else's behalf. */
function ApprovalCard({
  runId,
  approval,
  onDecided,
}: {
  runId: string;
  approval: GraphApproval;
  onDecided: (run: GraphRun) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const labels: Record<ApprovalDecision, string> = {
    accept: "Allow",
    accept_for_session: "Allow for this run",
    cancel: "Deny",
  };

  async function decide(decision: ApprovalDecision) {
    setBusy(true);
    setError("");
    try {
      onDecided(await decideGraphApproval(runId, approval.approvalId, decision));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      setBusy(false);
    }
  }

  return (
    <div className="approval-pending decision">
      <p>{approval.reason || "The agent is asking to do something."}</p>
      {approval.command && <code className="tool-detail">{approval.command}</code>}
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      <div className="decision-actions">
        {approval.allowedDecisions.map((decision) => (
          <button
            key={decision}
            type="button"
            className={`btn ${decision === "cancel" ? "" : "btn-primary"}`}
            disabled={busy}
            onClick={() => void decide(decision)}
          >
            {labels[decision] ?? decision}
          </button>
        ))}
      </div>
    </div>
  );
}

export function GraphRunDetailPage({ runId }: { runId: string }) {
  const { run: shown, events, error, setRun } = useGraphRun(runId);
  const { graphs } = useGraphTopologies();
  const topology = graphs.find((graph) => graph.graphId === shown?.graphId);
  const finished = useMemo(() => finishedNodes(events), [events]);
  const activity = useMemo(() => activityByNode(events), [events]);
  const statuses = nodeStatuses(topology, shown, finished);
  const pending = shown?.pendingApprovals ?? [];
  const decision = shown?.values.decision;
  const workspace = shown?.values.workspace;
  const task = shown?.values.task;

  return (
    <main className="panel-scroll">
      {error ? (
        <p className="notice notice-block">Could not load WorkOrder: {error}</p>
      ) : !shown ? (
        <p className="state-inline">Loading WorkOrder…</p>
      ) : (
        <>
          <header className="detail-head">
            <a href="/runs" className="back-link">
              ← All WorkOrders
            </a>
            <div className="detail-title">
              <div>
                <p className="eyebrow">{topology?.name ?? shown.graphId}</p>
                <h1>{String(task ?? shown.runId)}</h1>
              </div>
              <span
                className={`chip ${shown.status === "failed" ? "chip-flame" : "chip-ink"}`}
              >
                {graphRunLabel(topology, shown)}
              </span>
            </div>
          </header>
          <StatStrip>
            <Stat label="Run ID" value={shown.runId} />
            <Stat label="Repository" value={String(shown.values.repository ?? "—")} />
            <Stat
              label="Checkpoint"
              value={shown.checkpointId ? shown.checkpointId.slice(0, 8) : "—"}
            />
            <Stat label="Decision" value={String(decision ?? "In progress")} />
          </StatStrip>
          {typeof workspace === "string" && workspace && (
            <section className="run-workspace" aria-label="WorkOrder checkout">
              <div className="workspace-control">
                <span className="micro">Working in</span>
                <code className="dock-path">cd {workspace}</code>
              </div>
            </section>
          )}
          <ol className="stages" aria-label="Current WorkOrder stage">
            {(topology?.nodes ?? []).map((node) => (
              <li
                className="stage"
                data-status={statuses.get(node.nodeId) ?? "pending"}
                aria-current={
                  statuses.get(node.nodeId) === "in_progress" ||
                  statuses.get(node.nodeId) === "action_required"
                    ? "step"
                    : undefined
                }
                key={node.nodeId}
              >
                <span>{node.name}</span>
              </li>
            ))}
          </ol>
          {pending
            .filter((approval) => approval.toolName === HUMAN_REVIEW_TOOL)
            .map((approval) => (
              <section className="callout callout-action" key={approval.approvalId}>
                <p className="eyebrow">Action required</p>
                <h2>Human review</h2>
                <p>
                  The implementation and the agent review are complete. A human
                  approval or rejection is the final decision.
                </p>
                <HumanReviewDecision
                  runId={shown.runId}
                  approval={approval}
                  onDecided={setRun}
                />
              </section>
            ))}
          {decision && (
            <section
              className={`callout ${decision === "rejected" ? "callout-rejected" : ""}`}
            >
              <p className="eyebrow">Final human decision</p>
              <h2>{String(decision)}</h2>
              <p>
                {String(shown.values.decisionNote || "No decision summary was provided.")}
              </p>
            </section>
          )}
          {shown.error && <p className="notice notice-block">{shown.error}</p>}
          <section className="timeline" aria-label="WorkOrder steps">
            {(topology?.nodes ?? []).map((node) => {
              const status = statuses.get(node.nodeId) ?? "pending";
              const said = activity.get(node.nodeId);
              const output = shown.values[node.nodeId];
              const asked = pending.filter(
                (approval) =>
                  approval.nodeId === node.nodeId &&
                  approval.toolName !== HUMAN_REVIEW_TOOL,
              );
              return (
                <article
                  className={`step ${status === "in_progress" ? "step-current" : ""}`}
                  data-live={status === "in_progress" || undefined}
                  key={node.nodeId}
                >
                  <div className="step-rail" aria-hidden="true" />
                  <div className="step-body">
                    <header>
                      <div>
                        <span className="eyebrow">{node.kind} step</span>
                        <h2>{node.name}</h2>
                      </div>
                      <span
                        className={`chip ${status === "action_required" ? "chip-flame" : ""}`}
                      >
                        {status.replaceAll("_", " ")}
                      </span>
                    </header>
                    {node.description && (
                      <p className="step-summary">{node.description}</p>
                    )}
                    {said?.transcript.map((message, index) => (
                      <p className="step-summary" key={index} data-role={message.role}>
                        {message.text}
                      </p>
                    ))}
                    {said?.tools.map((call, index) => (
                      <code className="tool-detail" key={`${call.callId}-${index}`}>
                        {call.name}
                      </code>
                    ))}
                    {typeof output === "string" && output && (
                      <dl className="step-outputs">
                        <div>
                          <dt>{node.nodeId}</dt>
                          <dd>{output}</dd>
                        </div>
                      </dl>
                    )}
                    {asked.map((approval) => (
                      <ApprovalCard
                        key={approval.approvalId}
                        runId={shown.runId}
                        approval={approval}
                        onDecided={setRun}
                      />
                    ))}
                  </div>
                </article>
              );
            })}
          </section>
        </>
      )}
    </main>
  );
}

/** What `/runs` is in graph mode.
 *
 *  The control surface deliberately answers questions about *a* run rather than
 *  keeping a list of them, so there is nothing here to list. Rather than invent
 *  a listing the server does not have, the page says what it can start. */
export function GraphRunsPage() {
  const { graphs, error, loaded } = useGraphTopologies();
  return (
    <main className="panel-scroll">
      <header className="hero">
        <p className="eyebrow">OpenEngine / Work</p>
        <h1>WorkOrders</h1>
        <p className="lede">
          Each WorkOrder is a run of one of these graphs. Open one by its link, or start
          another.
        </p>
      </header>
      <div className="toolbar">
        <div className="toolbar-end">
          <a className="btn" href="/runs/new">
            New WorkOrder
          </a>
        </div>
      </div>
      {error ? (
        <p className="notice notice-block">Could not load graphs: {error}</p>
      ) : !loaded ? (
        <p className="state-inline">Loading graphs…</p>
      ) : (
        <div className="cards">
          {graphs.map((graph) => (
            <article className="card" key={graph.graphId}>
              <div className="card-top">
                <span className="chip">{runnerOf(graph)}</span>
                <code className="card-id">{graph.graphId}</code>
              </div>
              <h2>{graph.name}</h2>
              <dl className="card-stats">
                <div>
                  <dt>Stages</dt>
                  <dd>{graph.nodes.length}</dd>
                </div>
                <div>
                  <dt>Entry</dt>
                  <dd>{graph.entryPoint}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
