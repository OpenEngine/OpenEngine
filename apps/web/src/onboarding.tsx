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
  const [trusted, setTrusted] = useState(false);
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
        method: "POST", body: JSON.stringify({ repository: url, mode, trusted }),
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start onboarding");
    } finally { setSubmitting(false); }
  }

  return <section aria-label="Add repository">
    <header className="hero hero-narrow">
      <p className="eyebrow">OpenEngine / Repository</p>
      <h1>Add repository</h1>
      <p className="lede">Connect a repository to start creating WorkOrders.</p>
    </header>
    <form className="form" onSubmit={submit}>
      <label><span>GitHub URL</span><input required value={url} disabled={active}
        placeholder="https://github.com/owner/repo" onChange={(event) => setUrl(event.target.value)} /></label>
      <label><span>Mode</span><select value={mode} disabled={active} onChange={(event) => setMode(event.target.value)}>
        <option value="connected">Connected</option>
        <option value="disconnected">Disconnected</option>
      </select></label>
      <p className="form-note">Connected work can push branches and open pull requests. Disconnected work stays local.</p>
      {job && <p className="notice" role="status">{job.message}</p>}
      {job?.problems.map((problem) => <p className="notice" role="alert" key={problem.code}>
      {problem.message}. Fix: {problem.fix}
      </p>)}
      {error && <p className="notice" role="alert">{error}</p>}
      <label><input type="checkbox" checked={trusted} disabled={active} onChange={(event) => setTrusted(event.target.checked)} />Trust this repository: agents may edit files and run commands without asking</label>
      <div className="form-actions">
        <button className="btn btn-primary" type="submit" disabled={active}>{active ? "Adding repository…" : "Add repository"}</button>
      </div>
    </form>
  </section>;
}
