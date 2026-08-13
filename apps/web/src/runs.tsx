import { useEffect, useState } from "react";

import { api, type ApiRunStep, type ApiWorkflowRun } from "./api";

function phaseLabel(value: string) {
  return value.replaceAll("_", " ");
}

function RunNavigation({ runs }: { runs: ApiWorkflowRun[] }) {
  return (
    <aside className="sidebar run-sidebar">
      <a className="brand" href="/runs">
        <span className="brand-mark">e</span>
        <span>openengine</span>
      </a>
      <nav className="run-nav" aria-label="Work">
        <a className="run-nav-primary" href="/runs">Workflow runs</a>
        <div className="thread-list-label">Recent runs</div>
        {runs.map((run) => (
          <a className="run-nav-item" href={`/runs/${run.runId}`} key={run.runId}>
            <strong>{run.taskPrompt || run.runId}</strong>
            <span>{phaseLabel(run.phase)} · {run.workflowVersion || run.workflowId}</span>
          </a>
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
    api<ApiWorkflowRun>(`/api/runs/${encodeURIComponent(runId)}`)
      .then(setRun)
      .catch((reason: Error) => setError(reason.message));
  }, [runId]);

  return (
    <div className="app-shell">
      <RunNavigation runs={runs} />
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
