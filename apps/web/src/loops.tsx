/** Loops: the rail's list of them and the settings they share, the new loop
 *  form, and the page about one loop. */

import { useEffect, useState, type FormEvent } from "react";

import {
  createLoop,
  deleteLoop,
  getLoop,
  getLoopDefaults,
  getLoopSettings,
  listLoops,
  setLoopSettings,
  type EngineConfig,
  type Loop,
  type LoopDraft,
  type LoopSettings,
} from "./api";

const EXIT_CRITERIA_TOOLTIP = "Loops stop working if any one of their exit criteria are met.";

const STRATEGIES: { value: LoopSettings["runnerStrategy"]; label: string }[] = [
  { value: "least-utilized", label: "Least utilized" },
  { value: "round-robin", label: "Round robin" },
  { value: "manual", label: "Manual" },
];

export function LoopSettingsForm({ runners }: { runners: string[] }) {
  const [settings, setSettings] = useState<LoopSettings | null>(null);
  const [status, setStatus] = useState<"idle" | "saving" | "saved">("idle");
  const [error, setError] = useState("");
  useEffect(() => {
    getLoopSettings().then(setSettings, (reason: Error) => setError(reason.message));
  }, []);
  if (!settings) {
    return <p className="rail-note">{error || "Loading loop settings…"}</p>;
  }
  const change = (next: Partial<LoopSettings>) => {
    setSettings({ ...settings, ...next });
    setStatus("idle");
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setStatus("saving");
    setError("");
    // A manual choice left on its placeholder takes the first runner offered,
    // which is what the dropdown shows.
    const manual = settings.runnerStrategy === "manual";
    setLoopSettings({
      ...settings,
      implementationRunner: manual ? settings.implementationRunner || runners[0] || "" : "",
      reviewRunner: manual ? settings.reviewRunner || runners[0] || "" : "",
    }).then(
      (saved) => {
        setSettings(saved);
        setStatus("saved");
      },
      (reason: Error) => {
        setError(reason.message);
        setStatus("idle");
      },
    );
  };
  return (
    <form className="rail-loops" aria-label="Loop settings" onSubmit={submit}>
      <fieldset className="rail-loops-criteria">
        <legend className="rail-loops-heading">
          Exit criteria
          {/* Drawn by CSS like the WorkOrder mode tip; the click is kept from
              the legend so a tap opens the tip. */}
          <span className="info-tip" role="img" tabIndex={0}
            aria-label={EXIT_CRITERIA_TOOLTIP} data-tip={EXIT_CRITERIA_TOOLTIP}
            onClick={(event) => event.preventDefault()}>ⓘ</span>
        </legend>
        <fieldset className="rail-loops-hours">
          <legend className="settings-label">Active hours</legend>
          <input className="settings-input" type="time" aria-label="Active from" required
            value={settings.activeHours.start}
            onChange={(event) =>
              change({ activeHours: { ...settings.activeHours, start: event.target.value } })} />
          <span aria-hidden="true">–</span>
          <input className="settings-input" type="time" aria-label="Active until" required
            value={settings.activeHours.end}
            onChange={(event) =>
              change({ activeHours: { ...settings.activeHours, end: event.target.value } })} />
      </fieldset>
      <label>
        <span className="settings-label">Max PRs</span>
        <input className="settings-input" type="number" min={1} step={1} required
          value={settings.maxPrs}
          onChange={(event) => change({ maxPrs: event.target.valueAsNumber })} />
      </label>
      <label>
        <span className="settings-label">Max spend ($/day)</span>
        <input className="settings-input" type="number" min={0} step={0.01} required
          value={settings.maxDailySpend}
          onChange={(event) => change({ maxDailySpend: event.target.valueAsNumber })} />
      </label>
      </fieldset>
      <label>
        <span className="settings-label">Runner strategy</span>
        <select className="settings-input" value={settings.runnerStrategy}
          onChange={(event) =>
            change({ runnerStrategy: event.target.value as LoopSettings["runnerStrategy"] })}>
          {STRATEGIES.map((strategy) => (
            <option key={strategy.value} value={strategy.value}>{strategy.label}</option>
          ))}
        </select>
      </label>
      {settings.runnerStrategy === "manual" && (
        <>
          <label>
            <span className="settings-label">Implementer group</span>
            <select className="settings-input" value={settings.implementationRunner || runners[0]}
              onChange={(event) => change({ implementationRunner: event.target.value })}>
              {runners.map((runner) => <option key={runner} value={runner}>{runner}</option>)}
            </select>
          </label>
          <label>
            <span className="settings-label">Reviewer group</span>
            <select className="settings-input" value={settings.reviewRunner || runners[0]}
              onChange={(event) => change({ reviewRunner: event.target.value })}>
              {runners.map((runner) => <option key={runner} value={runner}>{runner}</option>)}
            </select>
          </label>
        </>
      )}
      {error && <p className="settings-status settings-status-error" role="alert">{error}</p>}
      <div className="settings-actions">
        <button className="settings-button settings-button-primary" type="submit"
          disabled={status === "saving"}>
          {status === "saving" ? "Saving…" : "Save"}
        </button>
        {status === "saved" && <span className="settings-status settings-status-ok">Saved</span>}
      </div>
    </form>
  );
}

/** The loops in the rail, read when the section first opens. */
export function LoopList({ activeLoopId }: { activeLoopId?: string }) {
  const [loops, setLoops] = useState<Loop[] | null>(null);
  useEffect(() => {
    listLoops().then(setLoops, () => setLoops([]));
  }, []);
  if (!loops?.length) return null;
  return (
    <nav className="rail-scroll" aria-label="Loops">
      {loops.map((loop) => (
        <div className="rail-item" key={loop.loopId}
          data-active={activeLoopId === loop.loopId || undefined}>
          <a className="rail-item-trigger" href={`/loops/${encodeURIComponent(loop.loopId)}`}>
            <span className="rail-item-title" data-clamp="">{loop.name}</span>
            <span className="rail-item-meta">
              {loop.running && <span className="rail-live" aria-label="Loop is running" />}
              {nextRunLabel(loop)}
            </span>
          </a>
        </div>
      ))}
    </nav>
  );
}

function nextRunLabel(loop: Loop): string {
  if (loop.running) return "Running now";
  if (loop.deferredUntil) return `After WorkOrder ${loop.deferredUntil} completes`;
  if (!loop.nextRunAt) return "Not scheduled";
  const at = new Date(loop.nextRunAt);
  return at.getTime() <= Date.now() ? "Due now" : at.toLocaleString();
}

function dollars(value: number): string {
  return `$${value.toFixed(2)}`;
}

/** Styled as the new WorkOrder form: the prompt and the exit criteria, which
 *  start from the rail's loop settings. */
export function NewLoopPage({ config }: { config: EngineConfig }) {
  const [draft, setDraft] = useState<LoopDraft>({
    name: "",
    repository: config.repositories[0]?.path ?? ".",
    prompt: "",
    everyMinutes: 60,
    activeHours: { start: "00:00", end: "00:00" },
    maxWorkOrders: 3,
    maxDailySpend: 0,
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    getLoopDefaults().then(
      (defaults) => setDraft((current) => ({ ...current, ...defaults })),
      () => {},
    );
  }, []);
  const change = (next: Partial<LoopDraft>) => setDraft({ ...draft, ...next });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const loop = await createLoop(draft);
      window.location.assign(`/loops/${encodeURIComponent(loop.loopId)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create loop");
      setSubmitting(false);
    }
  }

  return (
    <main className="panel-scroll">
      <header className="hero hero-narrow">
        <p className="eyebrow">OpenEngine / New Loop</p>
        <h1>Create a Loop</h1>
        <p className="lede">
          A standing prompt an agent comes back to, creating WorkOrders until one of its exit criteria is met.
        </p>
      </header>
      <form className="form" onSubmit={submit}>
        <label>
          <span>Name</span>
          <input required value={draft.name} onChange={(event) => change({ name: event.target.value })} />
        </label>
        <label>
          <span>Repository</span>
          <select required value={draft.repository}
            onChange={(event) => change({ repository: event.target.value })}>
            {config.repositories.map((repo) => (
              <option key={repo.name} value={repo.path}>{repo.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Run every (minutes)</span>
          <input type="number" min={1} step={1} required value={draft.everyMinutes}
            onChange={(event) => change({ everyMinutes: event.target.valueAsNumber })} />
        </label>
        <details className="workflow-inputs" open>
          <summary>
            Exit criteria
            <span className="info-tip" role="img" tabIndex={0}
              aria-label={EXIT_CRITERIA_TOOLTIP} data-tip={EXIT_CRITERIA_TOOLTIP}
              onClick={(event) => event.preventDefault()}>ⓘ</span>
          </summary>
          <label>
            <span>Active from</span>
            <input type="time" required value={draft.activeHours.start}
              onChange={(event) =>
                change({ activeHours: { ...draft.activeHours, start: event.target.value } })} />
          </label>
          <label>
            <span>Active until</span>
            <input type="time" required value={draft.activeHours.end}
              onChange={(event) =>
                change({ activeHours: { ...draft.activeHours, end: event.target.value } })} />
          </label>
          <label>
            <span>Max WorkOrders per day</span>
            <input type="number" min={1} step={1} required value={draft.maxWorkOrders}
              onChange={(event) => change({ maxWorkOrders: event.target.valueAsNumber })} />
          </label>
          <label>
            <span>Max spend ($/day)</span>
            <input type="number" min={0} step={0.01} required value={draft.maxDailySpend}
              onChange={(event) => change({ maxDailySpend: event.target.valueAsNumber })} />
          </label>
        </details>
        <label>
          <span>Loop prompt</span>
          <textarea required rows={9} value={draft.prompt}
            onChange={(event) => change({ prompt: event.target.value })}
            placeholder="Describe the standing task each run should look for and turn into WorkOrders." />
        </label>
        {error && <p className="notice" role="alert">{error}</p>}
        <div className="form-actions">
          <a className="back-link" href="/runs">Cancel</a>
          <button className="btn btn-primary" disabled={submitting} type="submit">
            {submitting ? "Creating…" : "Create Loop"}
          </button>
        </div>
        <p className="form-note">The loop first runs as soon as it is inside its active hours.</p>
      </form>
    </main>
  );
}

/** One loop: when it runs next, what it has spent today, and its WorkOrders. */
export function LoopPage({ loopId }: { loopId: string }) {
  const [loop, setLoop] = useState<Loop | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const read = () => getLoop(loopId).then(setLoop, (reason: Error) => setError(reason.message));
    read();
    const timer = window.setInterval(read, 15_000);
    return () => window.clearInterval(timer);
  }, [loopId]);
  if (!loop) return <main className="state">{error || "Loading loop…"}</main>;
  const remove = async () => {
    if (!window.confirm(`Delete the loop ${loop.name}?`)) return;
    await deleteLoop(loop.loopId);
    window.location.assign("/runs");
  };
  return (
    <main className="panel-scroll">
      <header className="detail-head">
        <p className="eyebrow">OpenEngine / Loop</p>
        <div className="detail-title">
          <h1>{loop.name}</h1>
          <button className="btn" type="button" onClick={() => void remove()}>Delete loop</button>
        </div>
        <p className="workorder-prompt-text">{loop.prompt}</p>
        <dl className="loop-facts">
          <div><dt>Next run</dt><dd>{nextRunLabel(loop)}</dd></div>
          <div>
            <dt>Spent today</dt>
            <dd>
              {dollars(loop.spentToday)}
              {loop.maxDailySpend > 0 && ` of ${dollars(loop.maxDailySpend)}`}
            </dd>
          </div>
          <div>
            <dt>WorkOrders created</dt>
            <dd>
              {loop.workOrders.length} created · at most {loop.maxWorkOrders} a day
            </dd>
          </div>
          <div>
            <dt>Active hours</dt>
            <dd>
              {loop.activeHours.start === loop.activeHours.end
                ? "Any time"
                : `${loop.activeHours.start}–${loop.activeHours.end}`} · every {loop.everyMinutes} min
            </dd>
          </div>
        </dl>
      </header>
      <section className="loop-workorders" aria-label="WorkOrders from this loop">
        <h2>WorkOrders</h2>
        {loop.workOrders.length === 0 ? (
          <p className="form-note">No WorkOrders created yet.</p>
        ) : (
          <ul>
            {loop.workOrders.map((workOrder) => (
              <li key={workOrder.runId}>
                <a href={`/runs/${encodeURIComponent(workOrder.runId)}`}>{workOrder.name}</a>
                <span className="chip">{workOrder.phase}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
