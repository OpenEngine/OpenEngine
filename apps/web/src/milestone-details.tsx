/** One milestone, opened up.
 *
 *  The timeline gives a workstream a bullet and the project's milestones page
 *  gives it a line under the goal it hangs from; neither says whether anything
 *  is happening in it. This is the page a workstream opens: every workstream
 *  under the milestone, and under each the tasks started in it -- what the task
 *  is, the stage its run has reached, and the way through to the run itself. */

import { useMemo } from "react";

import {
  projectMilestonesUrl,
  type ApiWorkflowRun,
  type ApiWorkstream,
} from "./api";
import { Stat, StatStrip } from "./brand";
import { useProjectMilestones } from "./milestone-timeline";
import { IN_PROGRESS_PHASES, phaseAccent, runStatusLabel } from "./runs";

/** The tasks under each workstream, in the order the runs list was sent.
 *
 *  Grouped from the run list the shell already polls rather than read per
 *  workstream: a run carries the workstream it was started in, so this page
 *  costs the plan it is already following and nothing more. */
function tasksByWorkstream(runs: ApiWorkflowRun[]): Map<string, ApiWorkflowRun[]> {
  const grouped = new Map<string, ApiWorkflowRun[]>();
  for (const run of runs) {
    if (!run.workstreamId) continue;
    const current = grouped.get(run.workstreamId);
    if (current) current.push(run);
    else grouped.set(run.workstreamId, [run]);
  }
  return grouped;
}

function activeTasks(tasks: ApiWorkflowRun[]) {
  return tasks.filter((task) => IN_PROGRESS_PHASES.has(task.phase)).length;
}

function WorkstreamCard({
  workstream,
  tasks,
}: {
  workstream: ApiWorkstream;
  tasks: ApiWorkflowRun[];
}) {
  const active = activeTasks(tasks);
  const titleId = `workstream-${workstream.workstreamId}`;
  return (
    <article className="card workstream-card" aria-labelledby={titleId}>
      <div className="card-top">
        <span className="chip-row">
          <span className="chip">
            {tasks.length} {tasks.length === 1 ? "task" : "tasks"}
          </span>
          {active > 0 && <span className="chip chip-flame">{active} active</span>}
        </span>
        <code className="card-id">{workstream.workstreamId}</code>
      </div>
      <h2 id={titleId}>{workstream.name}</h2>
      {workstream.scope && <p className="lede">{workstream.scope}</p>}
      {tasks.length > 0 ? (
        // Named in full rather than off the heading above it: two milestones in
        // one plan may both hang a workstream called "Web", and a list
        // answering to "Web" names neither of them.
        <ul className="workstream-tasks" aria-label={`Tasks in ${workstream.name}`}>
          {tasks.map((task) => (
            <li key={task.runId}>
              <a href={`/runs/${encodeURIComponent(task.runId)}`}>
                <span className="workstream-task-name">{task.name}</span>
                <span className="workstream-task-stage" data-accent={phaseAccent(task.phase)}>
                  {runStatusLabel(task)}
                </span>
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <p className="micro">No tasks have been started in this workstream yet.</p>
      )}
    </article>
  );
}

export function MilestoneDetailsPage({
  projectId,
  milestoneId,
  runs,
}: {
  projectId: string;
  milestoneId: string;
  /** Every workflow run the shell is following, which is where this page's
   *  tasks come from. */
  runs: ApiWorkflowRun[];
}) {
  const { project, milestones, loaded, error, stale } = useProjectMilestones(projectId);
  const milestone = milestones.find((item) => item.milestoneId === milestoneId);
  const names = useMemo(
    () => new Map(milestones.map((item) => [item.milestoneId, item.name])),
    [milestones],
  );
  const grouped = useMemo(() => tasksByWorkstream(runs), [runs]);
  const workstreams = milestone?.workstreams ?? [];
  const tasks = workstreams.flatMap(
    (workstream) => grouped.get(workstream.workstreamId) ?? [],
  );
  // The goals this one waits on, read as the names the planner gave them rather
  // than as the ids it recorded -- the same way the milestone's card does.
  const dependencies = (milestone?.dependencies ?? []).map((id) => names.get(id) ?? id);

  return (
    <main className="panel-scroll">
      <header className="detail-head">
        <a className="back-link" href={projectMilestonesUrl(projectId)}>
          ← All milestones
        </a>
        <div className="detail-title">
          <div>
            <p className="eyebrow">{project?.name ?? "Project"}</p>
            <h1>{milestone?.name ?? "Milestone"}</h1>
            {milestone?.description && <p className="lede">{milestone.description}</p>}
            {dependencies.length > 0 && (
              <p className="micro">Depends on {dependencies.join(" · ")}</p>
            )}
          </div>
          {stale && (
            <span className="micro milestone-stale" role="status">
              Not updating: {error}
            </span>
          )}
        </div>
      </header>
      {!loaded ? (
        error ? (
          // Only reached before the first answer; once the milestone is on
          // screen, a failure is reported by the header note instead.
          <p className="notice notice-block">Could not load milestone: {error}</p>
        ) : (
          <p className="state-inline">Loading milestone…</p>
        )
      ) : !milestone ? (
        // Reachable two ways: the URL is guessable, and `delete_milestone` can
        // take this goal out of the plan while its page is open and polling.
        <p className="notice notice-block">
          This project&rsquo;s plan has no milestone {milestoneId}.
        </p>
      ) : (
        <>
          <StatStrip>
            <Stat label="Workstreams" value={workstreams.length} />
            <Stat label="Tasks" value={tasks.length} />
            <Stat
              label="Active"
              value={activeTasks(tasks)}
              tone={activeTasks(tasks) ? "live" : undefined}
            />
          </StatStrip>
          {workstreams.length > 0 ? (
            <div className="cards">
              {workstreams.map((workstream) => (
                <WorkstreamCard
                  key={workstream.workstreamId}
                  workstream={workstream}
                  tasks={grouped.get(workstream.workstreamId) ?? []}
                />
              ))}
            </div>
          ) : (
            <div className="empty">
              <h2>No workstreams yet.</h2>
              <p>Work is planned under this milestone before any task can be started in it.</p>
            </div>
          )}
        </>
      )}
    </main>
  );
}
