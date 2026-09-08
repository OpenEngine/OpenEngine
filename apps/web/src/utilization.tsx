/** What each runner's subscription has been spent on, across providers.
 *
 *  Two reads on open, in this order: the cache, which answers at once and says
 *  what was true the last time anybody looked, and then the scrape, which asks
 *  every provider again. The cached figures are only painted if they arrive
 *  first -- a scrape that beats them is newer, and nothing older should land on
 *  top of it.
 *
 *  A provider that could not be read keeps its meters and gains a line saying
 *  why they are not newer, because "Codex is not signed in" is worth reading
 *  next to a week's usage rather than instead of it. */

import { useEffect, useState } from "react";

import {
  getUtilization,
  refreshUtilization,
  type ApiRunnerUtilization,
  type ApiUtilizationWindow,
} from "./api";
import { Stat, StatStrip } from "./brand";

/** A meter is drawn to the bar's own length; a figure past its limit still has
 *  to fit in the track it is drawn in. */
function clamp(percent: number): number {
  return Math.min(100, Math.max(0, percent));
}

function percentLabel(percent: number): string {
  return `${Math.round(percent)}%`;
}

/** When the window starts over, in the reader's own time zone. Empty for a
 *  provider that named no reset, which is what a window nothing has been spent
 *  in looks like. */
function resetLabel(resetsAt: string): string {
  if (!resetsAt) return "";
  const at = new Date(resetsAt);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
}

function readLabel(readAt: number): string {
  if (!readAt) return "";
  return new Date(readAt * 1000).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

function Meter({ window }: { window: ApiUtilizationWindow }) {
  const used = clamp(window.usedPercent);
  const reset = resetLabel(window.resetsAt);
  return (
    <div className="usage-window">
      <div className="usage-window-head">
        <span className="usage-window-label">{window.label}</span>
        <span className="usage-window-value">{percentLabel(window.usedPercent)}</span>
      </div>
      {/* The figure is printed above, so the bar is decoration and says so. */}
      <div aria-hidden="true" className="usage-track">
        <span
          className="usage-fill"
          data-tone={used >= 90 ? "alert" : used >= 70 ? "warn" : undefined}
          style={{ width: `${used}%` }}
        />
      </div>
      <span className="micro usage-window-reset">
        {reset ? `Resets ${reset}` : "No reset reported"}
      </span>
    </div>
  );
}

function RunnerCard({ reading }: { reading: ApiRunnerUtilization }) {
  const read = readLabel(reading.readAt);
  return (
    <section aria-label={`${reading.runner} utilization`} className="usage-card">
      <div className="usage-card-head">
        <h2>{reading.runner}</h2>
        {reading.plan && <span className="chip">{reading.plan}</span>}
      </div>
      {reading.windows.length > 0 ? (
        reading.windows.map((window) => <Meter key={window.windowId} window={window} />)
      ) : (
        <p className="micro">Nothing has been read from this runner yet.</p>
      )}
      {reading.error && <p className="notice usage-error">{reading.error}</p>}
      {read && <span className="micro usage-read-at">Read at {read}</span>}
    </section>
  );
}

export function UtilizationPage() {
  const [runners, setRunners] = useState<ApiRunnerUtilization[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(true);
  const [error, setError] = useState("");
  // Bumped by the Refresh button; every scrape is one run of the effect, so a
  // second click cannot leave two in flight writing over each other.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    let scraped = false;
    setRefreshing(true);
    setError("");
    // Only the first open has a cache worth waiting on: a refresh already has
    // the figures the cache holds on screen.
    if (attempt === 0)
      void getUtilization()
        .then((value) => {
          if (!current || scraped) return;
          setRunners(value.runners);
          setLoaded(true);
        })
        .catch(() => {});
    void refreshUtilization()
      .then((value) => {
        if (!current) return;
        scraped = true;
        setRunners(value.runners);
        setLoaded(true);
        setRefreshing(false);
      })
      .catch((reason: Error) => {
        if (!current) return;
        setError(reason.message);
        setRefreshing(false);
      });
    return () => {
      current = false;
    };
  }, [attempt]);

  const highest = runners.reduce(
    (most, reading) =>
      reading.windows.reduce((inner, window) => Math.max(inner, window.usedPercent), most),
    0,
  );
  // The newest of the readings on screen, which is what "as of" means when one
  // provider answered and another did not.
  const readAt = runners.reduce((latest, reading) => Math.max(latest, reading.readAt), 0);

  return (
    <main className="panel-scroll">
      <header className="hero">
        <p className="eyebrow">OpenEngine / Utilization</p>
        <h1>Runner utilization</h1>
        <p className="lede">
          What each runner's subscription has already been spent, read from the provider
          the runner signs in to.
        </p>
      </header>
      <StatStrip>
        <Stat label="Runners" value={runners.length} />
        <Stat
          label="Highest window"
          value={runners.length ? percentLabel(highest) : "—"}
          tone={highest >= 90 ? "alert" : undefined}
        />
        <Stat label="Status" value={refreshing ? "Reading…" : "Up to date"} />
        <Stat label="As of" value={readLabel(readAt) || "—"} />
      </StatStrip>
      <div className="toolbar">
        <span className="micro">
          Claude reports its five-hour and weekly windows; Codex reports its week.
        </span>
        <div className="toolbar-end">
          <button
            className="btn"
            disabled={refreshing}
            onClick={() => setAttempt((value) => value + 1)}
            type="button"
          >
            {refreshing ? "Reading…" : "Refresh"}
          </button>
        </div>
      </div>
      {error && <p className="notice notice-block">Could not read utilization: {error}</p>}
      {!loaded && !error ? (
        <p className="state-inline">Reading utilization…</p>
      ) : runners.length === 0 ? (
        <p className="state-inline">No runner on this deployment reports utilization.</p>
      ) : (
        <div className="usage-cards">
          {runners.map((reading) => (
            <RunnerCard key={reading.runner} reading={reading} />
          ))}
        </div>
      )}
    </main>
  );
}
