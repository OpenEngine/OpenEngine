/** One milestone, opened up.
 *
 *  The timeline gives a milestone a node and the project's milestones page
 *  gives it a card; neither says whether anything is happening in it. This page
 *  shows every task started under the goal. */

import { useMemo, useState } from "react";

import {
  startScheduledRun,
  milestoneNewTaskUrl,
  milestoneScopeUrl,
  projectMilestonesUrl,
  type ApiWorkflowRunListing,
} from "./api";
import { Stat, StatStrip } from "./brand";
import { useProjectMilestones } from "./milestone-timeline";
import { phaseAccent, runFinished, runStatusLabel } from "./runs";

/** Work still to come: every task the engine has not finished with, which
 *  includes one parked on a human review. `IN_PROGRESS_PHASES` would drop those
 *  -- it asks whether a run is moving, and a milestone blocked on the operator
 *  reading this page is the last thing to report as nothing left to do. */
function unfinishedTasks(tasks: ApiWorkflowRunListing[]) {
  return tasks.filter((task) => !runFinished(task)).length;
}

/** Those blocked on the operator rather than on the engine, counted apart the
 *  way the runs page counts them: it is the one number a reader can act on. */
function awaitingReview(tasks: ApiWorkflowRunListing[]) {
  return tasks.filter((task) => task.phase === "awaiting_human_review").length;
}

function TaskList({
  tasks,
  label,
  onStart,
  starting,
}: {
  tasks: ApiWorkflowRunListing[];
  label: string;
  onStart: (task: ApiWorkflowRunListing) => void;
  starting: string | null;
}) {
  return (
    <ul className="milestone-task-list" aria-label={label}>
      {tasks.map((task) => (
        <li key={task.runId}>
          <a href={task.phase === "scheduled" ? undefined : `/runs/${encodeURIComponent(task.runId)}`}>
            <span className="milestone-task-name">{task.name}</span>
            <span className="milestone-task-stage" data-accent={phaseAccent(task.phase)}>
              {runStatusLabel(task)}
            </span>
          </a>
          {task.phase === "scheduled" && (
            <button className="btn" type="button" disabled={starting !== null}
              aria-label={`Start ${task.name}`} onClick={() => onStart(task)}>
              {starting === task.runId ? "Starting…" : "Start"}
            </button>
          )}
        </li>
      ))}
    </ul>
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
  runs: ApiWorkflowRunListing[];
  runsError: string;
  runsLoaded: boolean;
}) {
  const [starting, setStarting] = useState<string | null>(null);
  const [startError, setStartError] = useState("");
  const [started, setStarted] = useState<Record<string, ApiWorkflowRunListing>>({});
  // Keep the successful response visible until the shell's next poll catches up.
  runs = useMemo(() => runs.map((run) =>
    run.phase === "scheduled" ? started[run.runId] ?? run : run,
  ), [runs, started]);
  async function start(task: ApiWorkflowRunListing) {
    if (starting !== null) return;
    setStarting(task.runId);
    setStartError("");
    try {
      const run = await startScheduledRun(task.runId);
      setStarted((current) => ({ ...current, [run.runId]: run }));
    } catch (failure) {
      setStartError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setStarting(null);
    }
  }
  const { project, milestones, loaded, error, stale } = useProjectMilestones(projectId);
  const milestone = milestones.find((item) => item.milestoneId === milestoneId);
  const names = useMemo(
    () => new Map(milestones.map((item) => [item.milestoneId, item.name])),
    [milestones],
  );
  const tasks = runs.filter((run) => run.milestoneId === milestoneId);
  // The goals this one waits on, read as the names the planner gave them rather
  // than as the ids it recorded.
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
              <>
                <a
                  className="btn"
                  href={milestoneScopeUrl(projectId, milestone.milestoneId)}
                >
                  Scope
                </a>
                <a
                  className="btn btn-primary"
                  href={milestoneNewTaskUrl(projectId, milestone.milestoneId)}
                >
                  New task
                </a>
              </>
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
          <section className="milestone-tasks" aria-labelledby="milestone-tasks-title">
            <div className="milestone-tasks-head">
              <div>
                <h2 id="milestone-tasks-title">Milestone tasks</h2>
              </div>
              <span className="milestone-tasks-actions">
                {runsLoaded && (
                  <span className="chip">
                    {tasks.length} {tasks.length === 1 ? "task" : "tasks"}
                  </span>
                )}
              </span>
            </div>
            {startError && <p className="notice" role="alert">Could not start workorder: {startError}</p>}
            {!runsLoaded ? (
              <p className="micro">
                {runsError ? `Could not load tasks: ${runsError}` : "Loading tasks…"}
              </p>
            ) : tasks.length > 0 ? (
              <TaskList
                tasks={tasks}
                label={`Tasks in ${milestone.name}`}
                onStart={(task) => void start(task)}
                starting={starting}
              />
            ) : (
              <p className="micro">No tasks have been started under this milestone yet.</p>
            )}
          </section>
        </>
      )}
    </main>
  );
}
