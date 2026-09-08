import { useState, type FormEvent } from "react";

import {
  projectMilestonesUrl,
  scopeMilestone,
  type ApiScopingPlan,
  type ApiWorkOrderSpec,
} from "./api";
import { useProjectMilestones } from "./milestone-timeline";

export const MILESTONE_SCOPING_PROMPT =
  "Break this milestone down into workorders, each workorder should be low-medium complexity unless a larger workorder is deemed necessary, with each change resulting in roughly a 500-1000 line change.";

function WorkOrderCard({ spec }: { spec: ApiWorkOrderSpec }) {
  return (
    <article className="scope-workorder">
      <p className="eyebrow">Create</p>
      <h3>{spec.name}</h3>
      <p>{spec.objective}</p>
      {spec.dependencies.length > 0 && (
        <p className="micro">Depends on {spec.dependencies.join(" · ")}</p>
      )}
      {spec.evidenceRequirements.length > 0 && (
        <ul className="scope-evidence" aria-label="Evidence requirements">
          {spec.evidenceRequirements.map((requirement) => (
            <li key={requirement}>{requirement}</li>
          ))}
        </ul>
      )}
    </article>
  );
}

function ScopingPlan({ plan }: { plan: ApiScopingPlan }) {
  return (
    <section className="scope-plan" aria-label="Scoping plan">
      <header>
        <p className="eyebrow">Engine scoper</p>
        <h2>Proposed work-order plan</h2>
      </header>
      {plan.reasons.length > 0 && (
        <ul className="scope-reasons" aria-label="Plan reasons">
          {plan.reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
      <div className="scope-plan-lanes">
        <section aria-labelledby="scope-create-heading">
          <h3 id="scope-create-heading">Create · {plan.create.length}</h3>
          <div className="scope-workorders">
            {plan.create.map((spec) => (
              <WorkOrderCard key={`${spec.milestoneId}:${spec.name}`} spec={spec} />
            ))}
          </div>
        </section>
        <section aria-labelledby="scope-cancel-heading">
          <h3 id="scope-cancel-heading">Cancel · {plan.cancel.length}</h3>
          <div className="scope-operation-list">
            {plan.cancel.map((workorderId) => (
              <code key={workorderId}>{workorderId}</code>
            ))}
          </div>
        </section>
        <section aria-labelledby="scope-supersede-heading">
          <h3 id="scope-supersede-heading">Supersede · {plan.supersede.length}</h3>
          <div className="scope-workorders">
            {plan.supersede.map((item) => (
              <article className="scope-workorder scope-supersession" key={item.workorderId}>
                <p className="eyebrow">Replace {item.workorderId}</p>
                {item.replacements.map((replacement) => (
                  <div key={`${replacement.milestoneId}:${replacement.name}`}>
                    <h4>{replacement.name}</h4>
                    <p>{replacement.objective}</p>
                  </div>
                ))}
              </article>
            ))}
          </div>
        </section>
      </div>
    </section>
  );
}

export function MilestoneScopePage({
  projectId,
  milestoneId,
}: {
  projectId: string;
  milestoneId: string;
}) {
  const { project, milestones, loaded, error: loadError } =
    useProjectMilestones(projectId);
  const milestone = milestones.find((item) => item.milestoneId === milestoneId);
  const [message, setMessage] = useState(MILESTONE_SCOPING_PROMPT);
  const [sentMessage, setSentMessage] = useState("");
  const [plan, setPlan] = useState<ApiScopingPlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function send(event: FormEvent) {
    event.preventDefault();
    const prompt = message.trim();
    if (!prompt || busy) return;
    setSentMessage(prompt);
    setPlan(undefined);
    setError("");
    setBusy(true);
    try {
      setPlan(await scopeMilestone(projectId, milestoneId, prompt));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="panel-scroll milestone-scope-page">
      <header className="detail-head">
        <a className="back-link" href={projectMilestonesUrl(projectId)}>
          ← All milestones
        </a>
        <div className="detail-title">
          <div>
            <p className="eyebrow">{project?.name ?? "Project"} · Milestone scoper</p>
            <h1>{milestone ? `Scope ${milestone.name}` : "Scope milestone"}</h1>
            {milestone?.description && <p className="lede">{milestone.description}</p>}
            {milestone && <code className="card-id">{milestone.milestoneId}</code>}
          </div>
        </div>
      </header>

      {!loaded && !loadError && <p className="state">Loading milestone…</p>}
      {loadError && <p className="state state-error">{loadError}</p>}
      {loaded && !milestone && <p className="state state-error">Milestone not found.</p>}

      {milestone && (
        <section className="scope-chat" aria-label={`Scope ${milestone.name}`}>
          {sentMessage && <p className="scope-message scope-message-user">{sentMessage}</p>}
          {busy && (
            <p className="scope-message scope-message-agent" role="status">
              Engine scoper is working…
            </p>
          )}
          {error && <p className="state state-error">{error}</p>}
          {plan && <ScopingPlan plan={plan} />}
          <form className="scope-composer" onSubmit={(event) => void send(event)}>
            <label htmlFor="milestone-scope-message">Message the scoper</label>
            <textarea
              id="milestone-scope-message"
              className="field-box"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={4}
            />
            <button className="btn btn-primary" type="submit" disabled={busy || !message.trim()}>
              {busy ? "Scoping…" : "Send"}
            </button>
          </form>
        </section>
      )}
    </main>
  );
}
