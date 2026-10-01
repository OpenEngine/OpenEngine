/** The Loops section of the rail: the limits and runner choice every loop
 *  runs under. */

import { useEffect, useState, type FormEvent } from "react";

import { getLoopSettings, setLoopSettings, type LoopSettings } from "./api";

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
