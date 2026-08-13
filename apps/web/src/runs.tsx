import { type FormEvent, useEffect, useState } from "react";

import { api, type ApiRunStep, type ApiWorkflowRun, type EngineConfig } from "./api";

function phaseLabel(value: string) {
  return value.replaceAll("_", " ");
}

function RunNavigation({
  runs,
  activeRunId,
  activeView = "runs",
}: {
  runs: ApiWorkflowRun[];
  activeRunId?: string;
  activeView?: "runs" | "new";
}) {
  return (
    <aside className="sidebar run-sidebar">
      <a className="brand" href="/runs">
        <span className="brand-mark">e</span>
        <span>openengine</span>
      </a>
      <nav className="run-nav" aria-label="Work">
        <a className={`run-nav-link ${activeView === "runs" ? "run-nav-primary" : "run-nav-secondary"}`} href="/runs">
          Workflow runs
        </a>
        <a className={`run-nav-link new-workflow-link ${activeView === "new" ? "run-nav-primary" : "run-nav-secondary"}`} href="/runs/new">
          + New workflow
        </a>
        <div className="thread-list-label">Recent runs</div>
        {runs.map((run) => (
          <div className="run-nav-group" key={run.runId}>
            <a className="run-nav-item" data-active={activeRunId === run.runId || undefined} href={`/runs/${run.runId}`}>
              <strong>{run.taskPrompt || run.runId}</strong>
              <span>{phaseLabel(run.phase)} · {run.workflowVersion || run.workflowId}</span>
            </a>
            {run.steps.some((step) => step.conversationUrl) && (
              <div className="run-conversations" aria-label={`Conversations for ${run.taskPrompt}`}>
                {run.steps.filter((step) => step.conversationUrl).map((step) => (
                  <a href={step.conversationUrl!} key={step.stepId}>{step.name} conversation</a>
                ))}
              </div>
            )}
          </div>
        ))}
      </nav>
      <a className="conversation-nav" href="/conversations">Standalone conversations</a>
      <div className="sidebar-foot"><span className="status-dot" /> Local openengine</div>
    </aside>
  );
}

function useRuns() {
  const [runs, setRuns] = useState<ApiWorkflowRun[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api<{ runs: ApiWorkflowRun[] }>("/api/runs")
      .then((value) => setRuns(value.runs))
      .catch((reason: Error) => setError(reason.message));
  }, []);
  return { runs, error };
}

export function RunsPage() {
  const { runs, error } = useRuns();
  return (
    <div className="app-shell">
      <RunNavigation runs={runs} />
      <main className="runs-panel">
        <header className="runs-hero">
          <span className="eyebrow">OPENENGINE / WORK</span>
          <h1>Workflow runs</h1>
          <p>Each run brings the task, agent steps, outputs, and human decision together.</p>
        </header>
        {error ? <p className="run-error">Could not load runs: {error}</p> : runs.length ? (
          <div className="run-grid">
            {runs.map((run) => (
              <a className="run-card" href={`/runs/${run.runId}`} key={run.runId}>
                <div className="run-card-top">
                  <span className={`phase phase-${run.phase}`}>{phaseLabel(run.phase)}</span>
                  <code>{run.runId}</code>
                </div>
                <h2>{run.taskPrompt}</h2>
                <p>{run.repository}</p>
                <footer>{run.workflowName} · {run.workflowVersion}</footer>
              </a>
            ))}
          </div>
        ) : (
          <div className="empty-runs"><h2>No workflow runs yet.</h2><p>Standalone conversations remain available from the sidebar.</p></div>
        )}
      </main>
    </div>
  );
}

export function NewWorkflowPage() {
  const { runs } = useRuns();
  const [prompt, setPrompt] = useState("");
  const [repository, setRepository] = useState(".");
  const [runners, setRunners] = useState<string[]>([]);
  const [runner, setRunner] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((config) => {
        setRunners(config.workflowRunners);
        setRunner(config.defaultWorkflowRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const run = await api<ApiWorkflowRun>("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          workflowId: "implementation-review-v1",
          prompt,
          repository,
          runner,
        }),
      });
      window.location.assign(`/runs/${encodeURIComponent(run.runId)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create workflow run");
      setSubmitting(false);
    }
  }

  return (
    <div className="app-shell">
      <RunNavigation runs={runs} activeView="new" />
      <main className="new-workflow-panel">
        <header className="new-workflow-header">
          <span className="eyebrow">OPENENGINE / NEW WORKFLOW</span>
          <h1>Start a workflow</h1>
          <p>Create one run that keeps its stages, agent conversations, outputs, and final human decision together.</p>
        </header>
        <form className="new-workflow-form" onSubmit={submit}>
          <label>
            <span>Workflow definition</span>
            <select disabled value="implementation-review-v1">
              <option value="implementation-review-v1">Implementation review · v1</option>
            </select>
          </label>
          <label>
            <span>Repository</span>
            <input required value={repository} onChange={(event) => setRepository(event.target.value)} placeholder="owner/repository or local path" />
          </label>
          <label>
            <span>Implementation runner</span>
            <select required value={runner} onChange={(event) => setRunner(event.target.value)}>
              {runners.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label>
            <span>Task prompt</span>
            <textarea required rows={9} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe what the implementation agent should change and what success looks like." />
          </label>
          {error && <p className="run-error" role="alert">{error}</p>}
          <div className="new-workflow-actions">
            <a href="/runs">Cancel</a>
            <button disabled={submitting || !runner} type="submit">{submitting ? "Creating…" : "Create workflow run"}</button>
          </div>
          <p className="form-note">The implementation starts after the run is created. Reviewer execution is not available yet.</p>
        </form>
      </main>
    </div>
  );
}

function StageProgress({ run }: { run: ApiWorkflowRun }) {
  const preparing = run.phase === "pending" || run.phase === "preparing_workspace";
  const stages = [
    {
      id: "workspace",
      name: run.phase === "pending" ? "Queued" : "Workspace",
      status: preparing ? "in_progress" : "completed",
    },
    ...run.steps.map((step) => ({ id: step.stepId, name: step.name, status: step.status })),
  ];
  return (
    <ol className="stage-progress" aria-label="Current workflow stage">
      {stages.map((stage) => (
        <li className={`stage-progress-${stage.status}`} aria-current={stage.status === "in_progress" || stage.status === "action_required" ? "step" : undefined} key={stage.id}>
          <span aria-hidden="true" className="stage-marker" />
          <span>{stage.name}</span>
        </li>
      ))}
    </ol>
  );
}

function StepCard({ step, current }: { step: ApiRunStep; current: boolean }) {
  return (
    <article className={`run-step ${current ? "run-step-current" : ""}`}>
      <div className="step-rail"><span /></div>
      <div className="step-body">
        <header>
          <div><span className="step-kind">{step.kind} step</span><h2>{step.name}</h2></div>
          <span className={`step-status step-status-${step.status}`}>{phaseLabel(step.status)}</span>
        </header>
        {step.outcome && (
          <p className={`step-outcome ${step.changesRequested ? "changes-requested" : ""}`}>
            Outcome: <strong>{phaseLabel(step.outcome)}</strong>
          </p>
        )}
        {step.summary && <p className="step-summary">{step.summary}</p>}
        {step.outputs.length > 0 && (
          <dl className="step-outputs">
            {step.outputs.map((output) => (
              <div key={output.name}><dt>{output.name}</dt><dd>{output.value}</dd></div>
            ))}
          </dl>
        )}
        {step.agentId && (
          <div className="agent-row">
            <span>Agent <strong>{step.agentId}</strong></span>
            {step.conversationUrl ? <a href={step.conversationUrl}>Open conversation →</a> : <span>Conversation not started</span>}
          </div>
        )}
      </div>
    </article>
  );
}

export function RunDetailPage({ runId }: { runId: string }) {
  const { runs } = useRuns();
  const [run, setRun] = useState<ApiWorkflowRun>();
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api<ApiWorkflowRun>(`/api/runs/${encodeURIComponent(runId)}`)
        .then((value) => {
          if (cancelled) return;
          setRun(value);
          setError("");
          if (["pending", "preparing_workspace", "implementing"].includes(value.phase)) {
            timer = window.setTimeout(load, 1000);
          }
        })
        .catch((reason: Error) => {
          if (!cancelled) setError(reason.message);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId]);

  return (
    <div className="app-shell">
      <RunNavigation runs={runs} activeRunId={runId} />
      <main className="run-detail-panel">
        {error ? <p className="run-error">Could not load run: {error}</p> : !run ? (
          <p className="loading-inline">Loading workflow run…</p>
        ) : (
          <>
            <header className="run-detail-header">
              <a href="/runs" className="back-link">← All workflow runs</a>
              <div className="run-title-row">
                <div><span className="eyebrow">{run.workflowName} / {run.workflowVersion}</span><h1>{run.taskPrompt}</h1></div>
                <span className={`phase phase-${run.phase}`}>{phaseLabel(run.phase)}</span>
              </div>
              <dl className="run-facts">
                <div><dt>Run ID</dt><dd><code>{run.runId}</code></dd></div>
                <div><dt>Workflow</dt><dd>{run.workflowId}</dd></div>
                <div><dt>Repository</dt><dd>{run.repository}</dd></div>
                <div><dt>Current step</dt><dd>{run.currentStepId ?? "—"}</dd></div>
                <div><dt>Final outcome</dt><dd>{run.terminalOutcome ?? "In progress"}</dd></div>
              </dl>
            </header>
            <StageProgress run={run} />
            {run.pendingHumanReview && (
              <section className="pending-action">
                <span className="eyebrow">ACTION REQUIRED</span>
                <h2>{run.pendingHumanReview.title}</h2>
                <p>The implementation and agent review are complete. A human approval or rejection is the final decision.</p>
              </section>
            )}
            {run.humanDecision && (
              <section className={`human-decision decision-${run.humanDecision.outcome}`}>
                <span className="eyebrow">FINAL HUMAN DECISION</span>
                <h2>{run.humanDecision.outcome}</h2>
                <p>{run.humanDecision.summary || "No decision summary was provided."}</p>
              </section>
            )}
            {run.failureReason && <p className="run-error">{run.failureReason}</p>}
            <section className="run-timeline" aria-label="Workflow steps">
              {run.steps.map((step) => <StepCard key={step.stepId} step={step} current={run.currentStepId === step.stepId} />)}
            </section>
          </>
        )}
      </main>
    </div>
  );
}
