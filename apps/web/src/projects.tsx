import { useEffect, useState, type FormEvent } from "react";
import { api, type EngineConfig } from "./api";

type ProjectFields = {
  name: string; repository: string; workflow: string; instructions: string;
  timezone: string; weekdays: number[]; start_time: string; end_time: string;
  daily_budget: number; enabled: boolean;
};
export type ApiProject = ProjectFields & {
  project_id: string; used_budget: number; last_run_id: string; error: string;
};
const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function ProjectsSidebar() {
  const [projects, setProjects] = useState<ApiProject[]>([]);
  const [config, setConfig] = useState<EngineConfig>();
  const [editing, setEditing] = useState<ApiProject | "new" | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    async function refresh() {
      try {
        const value = await api<ApiProject[]>("/api/projects");
        if (current) { setProjects(value); setError(""); }
      } catch (reason) {
        if (current) setError(reason instanceof Error ? reason.message : "Could not load projects");
      }
    }
    void refresh();
    void api<EngineConfig>("/api/config").then((value) => {
      if (current) setConfig(value);
    }).catch((reason) => { if (current) setError(String(reason)); });
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => { current = false; window.clearInterval(timer); };
  }, []);

  return <div className="rail-scroll projects-panel">
    {error && <p role="alert">{error}</p>}
    {editing && config ? <ProjectEditor key={editing === "new" ? "new" : editing.project_id}
      project={editing === "new" ? undefined : editing} config={config}
      onCancel={() => setEditing(null)} onSaved={(saved) => {
        setProjects((items) => [...items.filter((item) => item.project_id !== saved.project_id), saved]);
        setEditing(null);
      }} /> : <>
      <button className="rail-button rail-button-primary" type="button"
        disabled={!config} onClick={() => setEditing("new")}>+ New project</button>
      <p className="rail-note">Schedule WorkOrders during your working hours.</p>
      {projects.map((project) => <div className="rail-group" key={project.project_id}>
        <button className="rail-item-trigger" type="button" onClick={() => setEditing(project)}>
          <span className="rail-item-title">{project.name}</span>
          <span className="rail-item-meta">{project.enabled ? "Scheduled" : "Paused"} · {project.used_budget}/{project.daily_budget} WorkOrders today</span>
          <span className="rail-item-meta">{project.start_time}–{project.end_time} · {project.timezone}</span>
        </button>
        {project.error && <p role="alert">{project.error}</p>}
        {project.last_run_id && <a className="rail-button" href={`/runs/${encodeURIComponent(project.last_run_id)}`}>Latest WorkOrder</a>}
      </div>)}
    </>}
  </div>;
}

function ProjectEditor({ project, config, onSaved, onCancel }: {
  project?: ApiProject; config: EngineConfig;
  onSaved: (project: ApiProject) => void; onCancel: () => void;
}) {
  const [fields, setFields] = useState<ProjectFields>(() => ({
    name: project?.name ?? "", repository: project?.repository ?? config.repositories[0]?.path ?? "",
    workflow: project?.workflow ?? config.workflows[0]?.id ?? "", instructions: project?.instructions ?? "",
    timezone: project?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
    weekdays: project?.weekdays ?? [0, 1, 2, 3, 4], start_time: project?.start_time ?? "09:00",
    end_time: project?.end_time ?? "17:00", daily_budget: project?.daily_budget ?? 1,
    enabled: project?.enabled ?? false,
  }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function update<K extends keyof ProjectFields>(key: K, value: ProjectFields[K]) {
    setFields((current) => ({ ...current, [key]: value }));
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      onSaved(await api<ApiProject>(project ? `/api/projects/${encodeURIComponent(project.project_id)}` : "/api/projects", {
        method: project ? "PATCH" : "POST", body: JSON.stringify(fields),
      }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not save project"); }
    finally { setBusy(false); }
  }
  return <form className="project-form" onSubmit={(event) => void save(event)}>
    <h2>{project ? "Edit project" : "New project"}</h2>
    <fieldset disabled={busy}>
      <label>Name<input required value={fields.name} onChange={(e) => update("name", e.target.value)} /></label>
      <label>Repository<select required value={fields.repository} onChange={(e) => update("repository", e.target.value)}>
        {config.repositories.map((repo) => <option key={repo.path} value={repo.path}>{repo.name}</option>)}
      </select></label>
      <label>Workflow<select required value={fields.workflow} onChange={(e) => update("workflow", e.target.value)}>
        {config.workflows.map((workflow) => <option key={workflow.id} value={workflow.id}>{workflow.name}</option>)}
      </select></label>
      <label>Agent instructions<textarea required rows={6} value={fields.instructions} onChange={(e) => update("instructions", e.target.value)} /></label>
      <label>Timezone<input required value={fields.timezone} placeholder="America/Denver" onChange={(e) => update("timezone", e.target.value)} /></label>
      <fieldset className="project-days"><legend>Working days</legend>{days.map((day, index) => <label key={day}>
        <input type="checkbox" checked={fields.weekdays.includes(index)} onChange={(e) => update("weekdays",
          e.target.checked ? [...fields.weekdays, index] : fields.weekdays.filter((value) => value !== index))} />{day}
      </label>)}</fieldset>
      <label>Start time<input type="time" required value={fields.start_time} onChange={(e) => update("start_time", e.target.value)} /></label>
      <label>End time<input type="time" required value={fields.end_time} onChange={(e) => update("end_time", e.target.value)} /></label>
      <label>Daily budget (WorkOrders)<input type="number" required min={1} max={100} step={1} value={fields.daily_budget} onChange={(e) => update("daily_budget", Number(e.target.value))} /></label>
      <p className="rail-note">Starts at most this many WorkOrders per local day, one at a time. Overnight hours end the next day. Pausing stops new starts; active work continues.</p>
      <label className="project-enabled"><input type="checkbox" checked={fields.enabled} onChange={(e) => update("enabled", e.target.checked)} />Enable autonomous scheduling</label>
      {error && <p role="alert">{error}</p>}
      <button className="rail-button rail-button-primary" type="submit">{busy ? "Saving…" : "Save project"}</button>
      <button className="rail-button" type="button" onClick={onCancel}>Cancel</button>
    </fieldset>
  </form>;
}
