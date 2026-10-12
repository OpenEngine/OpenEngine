import { type FormEvent, useEffect, useState } from "react";
import { api, type EngineConfig } from "./api";

export type OnboardingState = {
  id?: string;
  repository: string;
  path: string;
  status: string;
  message: string;
  problems: { code: string; message: string; fix: string }[];
};

export function AddRepository({ onComplete }: {
  onComplete: (config: EngineConfig, path: string) => void;
}) {
  const [url, setUrl] = useState("");
  const [mode, setMode] = useState("connected");
  const [job, setJob] = useState<OnboardingState | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const active = submitting || !!job && !["complete", "failed"].includes(job.status);

  useEffect(() => {
    if (!job || job.status === "failed") return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        if (job.status === "complete") {
          const config = await api<EngineConfig>("/api/config");
          if (!cancelled) onComplete(config, job.path);
        } else {
          const next = await api<OnboardingState>(`/api/repositories/onboard/${job.id}`);
          if (!cancelled) setJob(next);
        }
      } catch (reason) {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Could not read onboarding progress");
          setJob(null);
        }
      }
    }, job.status === "complete" ? 0 : 1000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [job, onComplete]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      setJob(await api<OnboardingState>("/api/repositories/onboard", {
        method: "POST", body: JSON.stringify({ repository: url, mode }),
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start onboarding");
    } finally { setSubmitting(false); }
  }

  return <section aria-label="Add repository">
    <h2>Add repository</h2>
    <form className="workflow-form" onSubmit={submit}>
      <label>GitHub URL<input required value={url} disabled={active}
        placeholder="https://github.com/owner/repo" onChange={(event) => setUrl(event.target.value)} /></label>
      <label>Mode<select value={mode} disabled={active} onChange={(event) => setMode(event.target.value)}>
        <option value="connected">Connected</option>
        <option value="disconnected">Disconnected</option>
      </select></label>
      <p>Connected work can push branches and open pull requests. Disconnected work stays local.</p>
      <button type="submit" disabled={active}>{active ? "Adding repository…" : "Add repository"}</button>
    </form>
    {job && <p role="status">{job.message}</p>}
    {job?.problems.map((problem) => <p role="alert" key={problem.code}>
      {problem.message}. Fix: {problem.fix}
    </p>)}
    {error && <p role="alert">{error}</p>}
  </section>;
}
