/** One milestone, opened up.
 *
 *  The timeline gives a workstream a bullet and the project's milestones page
 *  gives it a line under the goal it hangs from; neither says whether anything
 *  is happening in it. This page shows every task rolled up to the milestone,
 *  and lets a workstream narrow that list to the tasks started under it. */

import { useMemo, useState } from "react";

import {
  milestoneNewTaskUrl,
  projectMilestonesUrl,
  type ApiWorkflowRun,
  type ApiWorkstream,
} from "./api";
import { Stat, StatStrip } from "./brand";
import { useProjectMilestones } from "./milestone-timeline";
import { phaseAccent, runFinished, runStatusLabel } from "./runs";

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

/** Work still to come: every task the engine has not finished with, which
 *  includes one parked on a human review. `IN_PROGRESS_PHASES` would drop those
 *  -- it asks whether a run is moving, and a milestone blocked on the operator
 *  reading this page is the last thing to report as nothing left to do. */
function unfinishedTasks(tasks: ApiWorkflowRun[]) {
  return tasks.filter((task) => !runFinished(task)).length;
}

/** Those blocked on the operator rather than on the engine, counted apart the
 *  way the runs page counts them: it is the one number a reader can act on. */
function awaitingReview(tasks: ApiWorkflowRun[]) {
  return tasks.filter((task) => task.phase === "awaiting_human_review").length;
}

function TaskList({
  tasks,
  label,
  workstreamNames,
}: {
  tasks: ApiWorkflowRun[];
  label: string;
  workstreamNames: Map<string, string>;
}) {
  return (
    <ul className="workstream-tasks" aria-label={label}>
      {tasks.map((task) => (
        <li key={task.runId}>
          <a href={`/runs/${encodeURIComponent(task.runId)}`}>
            <span className="workstream-task-name">{task.name}</span>
            <span className="workstream-task-meta">
              <span className="workstream-task-context">
                {task.workstreamId
                  ? (workstreamNames.get(task.workstreamId) ?? task.workstreamId)
                  : "Milestone task"}
              </span>
              <span className="workstream-task-stage" data-accent={phaseAccent(task.phase)}>
                {runStatusLabel(task)}
              </span>
            </span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function WorkstreamCard({
  workstream,
  tasks,
  tasksKnown,
  selected,
  onSelect,
}: {
  workstream: ApiWorkstream;
  tasks: ApiWorkflowRun[];
  /** False until a poll of the run list has answered. An empty list is then
   *  the fact that nothing was started here; before it, it is only the state
   *  the shell began with, and saying "nothing yet" would be a claim made from
   *  data the page does not have. */
  tasksKnown: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const unfinished = unfinishedTasks(tasks);
  const awaiting = awaitingReview(tasks);
  const titleId = `workstream-${workstream.workstreamId}`;
  return (
    <article
      className="card workstream-card"
      aria-labelledby={titleId}
      data-selected={selected || undefined}
    >
      <button
        type="button"
        className="workstream-card-action"
        aria-labelledby={titleId}
        aria-pressed={selected}
        aria-controls="milestone-task-list"
        onClick={onSelect}
      />
      <div className="card-top">
        <span className="chip-row">
          {tasksKnown && (
            <>
              <span className="chip">
                {tasks.length} {tasks.length === 1 ? "task" : "tasks"}
              </span>
              {unfinished > 0 && <span className="chip">{unfinished} unfinished</span>}
              {awaiting > 0 && <span className="chip chip-flame">{awaiting} awaiting review</span>}
            </>
          )}
        </span>
        <code className="card-id">{workstream.workstreamId}</code>
      </div>
      <h2 id={titleId}>{workstream.name}</h2>
      {workstream.scope && <p className="lede">{workstream.scope}</p>}
    </article>
  );
}

export function MilestoneDetailsPage({
  projectId,
  milestoneId,
  runs,
  runsError,
  runsLoaded,
}: {
  projectId: string;
  milestoneId: string;
  /** Every workflow run the shell is following, which is where this page's
   *  tasks come from -- along with how that poll is faring, so a run list that
   *  has not arrived is not read as a milestone nothing was started under.
   *  Required rather than defaulted: a caller that forgot would leave the page
   *  loading forever, which is exactly the state these are here to end. */
  runs: ApiWorkflowRun[];
  runsError: string;
  runsLoaded: boolean;
}) {
  const [selectedWorkstreamId, setSelectedWorkstreamId] = useState<string | null>(null);
  const { project, milestones, loaded, error, stale } = useProjectMilestones(projectId);
  const milestone = milestones.find((item) => item.milestoneId === milestoneId);
  const names = useMemo(
    () => new Map(milestones.map((item) => [item.milestoneId, item.name])),
    [milestones],
  );
  const grouped = useMemo(() => tasksByWorkstream(runs), [runs]);
  const workstreams = milestone?.workstreams ?? [];
  const workstreamNames = useMemo(
    () => new Map(workstreams.map((workstream) => [workstream.workstreamId, workstream.name])),
    [workstreams],
  );
  const tasks = runs.filter(
    (run) =>
      run.milestoneId === milestoneId ||
      (run.workstreamId !== null && workstreamNames.has(run.workstreamId)),
  );
  // A workstream can disappear while this page is polling. Treat its old
  // selection as cleared immediately, so it cannot leave the task list pinned
  // to a heading no longer in the plan.
  const selectedWorkstream = workstreams.find(
    (workstream) => workstream.workstreamId === selectedWorkstreamId,
  );
  const visibleTasks = selectedWorkstream
    ? (grouped.get(selectedWorkstream.workstreamId) ?? [])
    : tasks;
  // The goals this one waits on, read as the names the planner gave them rather
  // than as the ids it recorded -- the same way the milestone's card does.
  const dependencies = (milestone?.dependencies ?? []).map((id) => names.get(id) ?? id);
  // Two polls feed this page and either can fall over on its own. Whichever it
  // is, the page holds what it last read and says so, rather than letting the
  // half still arriving make the other look current.
  const notUpdating = stale ? error : runsLoaded ? runsError : "";

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
          <div className="detail-actions">
            {notUpdating && (
              <span className="micro milestone-stale" role="status">
                Not updating: {notUpdating}
              </span>
            )}
            {milestone && (
              <a
                className="btn btn-primary"
                href={milestoneNewTaskUrl(projectId, milestone.milestoneId)}
              >
                New task
              </a>
            )}
          </div>
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
            {/* An em dash rather than a nought while the run list is out: this
                strip counts what the browser was actually sent, and a zero it
                was not sent would be the one figure here that could be wrong. */}
            <Stat label="Tasks" value={runsLoaded ? tasks.length : "—"} />
            <Stat
              label="Unfinished"
              value={runsLoaded ? unfinishedTasks(tasks) : "—"}
              tone={runsLoaded && unfinishedTasks(tasks) ? "live" : undefined}
            />
            <Stat
              label="Awaiting review"
              value={runsLoaded ? awaitingReview(tasks) : "—"}
              tone={runsLoaded && awaitingReview(tasks) ? "alert" : undefined}
            />
          </StatStrip>
          {workstreams.length > 0 ? (
            <div className="cards">
              {workstreams.map((workstream) => (
                <WorkstreamCard
                  key={workstream.workstreamId}
                  workstream={workstream}
                  tasks={grouped.get(workstream.workstreamId) ?? []}
                  tasksKnown={runsLoaded}
                  selected={selectedWorkstream?.workstreamId === workstream.workstreamId}
                  onSelect={() =>
                    setSelectedWorkstreamId((current) =>
                      current === workstream.workstreamId ? null : workstream.workstreamId,
                    )
                  }
                />
              ))}
            </div>
          ) : (
            <div className="empty">
              <h2>No workstreams yet.</h2>
              <p>Tasks can be created directly under this milestone.</p>
            </div>
          )}
          <section
            id="milestone-task-list"
            className="milestone-tasks"
            aria-labelledby="milestone-tasks-title"
          >
            <div className="milestone-tasks-head">
              <div>
                <h2 id="milestone-tasks-title">
                  {selectedWorkstream ? `${selectedWorkstream.name} tasks` : "Milestone tasks"}
                </h2>
                {selectedWorkstream && <p className="micro">Filtered by workstream</p>}
              </div>
              <span className="milestone-tasks-actions">
                {selectedWorkstream && (
                  <button
                    type="button"
                    className="btn btn-quiet"
                    onClick={() => setSelectedWorkstreamId(null)}
                  >
                    Show all tasks
                  </button>
                )}
                {runsLoaded && (
                  <span className="chip">
                    {visibleTasks.length} {visibleTasks.length === 1 ? "task" : "tasks"}
                  </span>
                )}
              </span>
            </div>
            {!runsLoaded ? (
              <p className="micro">
                {runsError ? `Could not load tasks: ${runsError}` : "Loading tasks…"}
              </p>
            ) : visibleTasks.length > 0 ? (
              <TaskList
                tasks={visibleTasks}
                label={
                  selectedWorkstream
                    ? `Tasks in ${selectedWorkstream.name}`
                    : `Tasks in ${milestone.name}`
                }
                workstreamNames={workstreamNames}
              />
            ) : selectedWorkstream ? (
              <p className="micro">No tasks have been started in this workstream yet.</p>
            ) : (
              <p className="micro">No tasks have been started under this milestone yet.</p>
            )}
          </section>
        </>
      )}
    </main>
  );
}
