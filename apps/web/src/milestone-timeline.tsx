import { useEffect, useMemo, useState } from "react";

import {
  getProjectMilestones,
  type ApiMilestone,
  type ApiProject,
} from "./api";

const NODE_GAP = 210;
const GRAPH_WIDTH = 1000;
const MIN_GRAPH_WIDTH = 640;
const TOOLTIP_EDGE_SPACE = 152;
// Keep a 280px tooltip plus a 12px gutter inside the smallest graph.
const SIDE_PADDING = (TOOLTIP_EDGE_SPACE / MIN_GRAPH_WIDTH) * GRAPH_WIDTH;
const NODE_Y = 96;
const POLL_MS = 1000;
// One failed poll is a blip and is kept quiet; a run of them is an outage, and
// a timeline that has stopped following the plan has to say so.
const STALE_AFTER_FAILURES = 3;

/** Whether two polls returned the same plan, compared as the server sent it. */
function sameMilestones(a: ApiMilestone[], b: ApiMilestone[]) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Keep every dependency to the left of the milestone that needs it.
 *
 * The store's order is used as the stable tie-breaker for unrelated goals.
 * Cycles are not valid planning data, but the guard still lets the page render
 * an old or hand-edited record instead of recursing forever. */
export function orderMilestones(milestones: ApiMilestone[]): ApiMilestone[] {
  const byId = new Map(milestones.map((milestone) => [milestone.milestoneId, milestone]));
  const ordered: ApiMilestone[] = [];
  const visited = new Set<string>();
  const visiting = new Set<string>();

  const visit = (milestone: ApiMilestone) => {
    if (visited.has(milestone.milestoneId)) return;
    if (visiting.has(milestone.milestoneId)) return;
    visiting.add(milestone.milestoneId);
    for (const dependency of milestone.dependencies) {
      const item = byId.get(dependency);
      if (item) visit(item);
    }
    visiting.delete(milestone.milestoneId);
    visited.add(milestone.milestoneId);
    ordered.push(milestone);
  };

  milestones.forEach(visit);
  return ordered;
}

export function MilestoneTimelineVisual({ milestones }: { milestones: ApiMilestone[] }) {
  const ordered = useMemo(() => orderMilestones(milestones), [milestones]);
  const positions = useMemo(
    () =>
      new Map(
        ordered.map((milestone, index) => [
          milestone.milestoneId,
          ordered.length === 1
            ? GRAPH_WIDTH / 2
            : SIDE_PADDING + index * ((GRAPH_WIDTH - SIDE_PADDING * 2) / (ordered.length - 1)),
        ]),
      ),
    [ordered],
  );
  const indexes = useMemo(
    () => new Map(ordered.map((milestone, index) => [milestone.milestoneId, index])),
    [ordered],
  );
  const minWidth = Math.max(
    MIN_GRAPH_WIDTH,
    (Math.max(0, ordered.length - 1) * NODE_GAP * GRAPH_WIDTH) /
      (GRAPH_WIDTH - SIDE_PADDING * 2),
  );

  if (!ordered.length)
    return <p className="milestone-empty">No milestones have been added to this project yet.</p>;

  return (
    <div className="milestone-map" style={{ minWidth }}>
      <svg
        className="milestone-lines"
        viewBox={`0 0 ${GRAPH_WIDTH} 180`}
        aria-hidden="true"
        preserveAspectRatio="none"
      >
        <defs>
          <marker
            id="milestone-arrow"
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="7"
            markerHeight="7"
            orient="auto"
          >
            <path d="M 0 0 L 8 4 L 0 8 z" />
          </marker>
        </defs>
        {ordered.flatMap((milestone) => {
          const target = positions.get(milestone.milestoneId)!;
          return milestone.dependencies.flatMap((dependency) => {
            const source = positions.get(dependency);
            if (source === undefined) return [];
            const span = Math.max(
              1,
              Math.abs(indexes.get(milestone.milestoneId)! - indexes.get(dependency)!),
            );
            const arch = Math.max(18, NODE_Y - 22 - span * 14);
            return (
              <path
                key={`${dependency}:${milestone.milestoneId}`}
                className="milestone-dependency"
                data-from={dependency}
                data-to={milestone.milestoneId}
                d={`M ${source + 13} ${NODE_Y} C ${source + 55} ${arch}, ${target - 55} ${arch}, ${target - 13} ${NODE_Y}`}
                markerEnd="url(#milestone-arrow)"
              />
            );
          });
        })}
      </svg>
      {ordered.map((milestone) => {
        const tooltipId = `milestone-description-${milestone.milestoneId}`;
        return (
          <div
            key={milestone.milestoneId}
            className="milestone-node"
            style={{ left: `${positions.get(milestone.milestoneId)! / 10}%` }}
            tabIndex={0}
            aria-describedby={milestone.description ? tooltipId : undefined}
          >
            {milestone.description && (
              <span className="milestone-tooltip" id={tooltipId} role="tooltip">
                {milestone.description}
              </span>
            )}
            <span className="milestone-dot" aria-hidden="true" />
            <span className="milestone-name">{milestone.name}</span>
          </div>
        );
      })}
    </div>
  );
}

/** The timeline of the project this conversation is planning, kept current.
 *
 *  Milestones are written by the planning tools in whatever process is running
 *  the agent, so the page re-reads the list rather than waiting to be told --
 *  the same poll the shell already runs for projects and workflow runs. */
export function MilestoneTimeline({ project }: { project?: ApiProject }) {
  const [milestones, setMilestones] = useState<ApiMilestone[]>([]);
  // Held apart from an empty list so a failed poll can keep showing the last
  // good timeline rather than replace it with an error. It also gates the
  // previous project's plan on a switch, which is deliberately not cleared.
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [failures, setFailures] = useState(0);

  useEffect(() => {
    setLoaded(false);
    setError("");
    setFailures(0);
    if (!project) {
      setMilestones([]);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    const load = () => {
      void getProjectMilestones(project.projectId, controller.signal)
        .then((value) => {
          // Guarded like the two handlers below: whatever this poll answers
          // belongs to the project that asked for it, not the one now shown.
          if (controller.signal.aborted) return;
          // Keep the previous array when the plan has not moved, so the
          // ordering and position memos hold and a steady-state poll re-renders
          // nothing rather than redrawing the whole graph once a second.
          setMilestones((current) =>
            sameMilestones(current, value.milestones) ? current : value.milestones,
          );
          setLoaded(true);
          setError("");
          setFailures(0);
        })
        .catch((reason: Error) => {
          if (controller.signal.aborted) return;
          setError(reason.message);
          setFailures((count) => count + 1);
        })
        .finally(() => {
          if (!controller.signal.aborted) timer = window.setTimeout(load, POLL_MS);
        });
    };
    load();
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [project?.projectId]);

  // A timeline that looks live but has stopped following the plan is the worst
  // thing this can be, so a run of failures is said out loud beside a render
  // that is now only the last known plan.
  const stale = loaded && failures >= STALE_AFTER_FAILURES;

  return (
    <section className="milestone-timeline" aria-labelledby="milestone-timeline-title">
      <header className="milestone-timeline-head">
        <div>
          <p className="eyebrow">Active project</p>
          <h2 id="milestone-timeline-title">Milestone timeline</h2>
        </div>
        <div className="milestone-timeline-status">
          <span className="micro">{project?.name ?? "Waiting for the first turn"}</span>
          {stale && (
            <span className="micro milestone-stale" role="status">
              Not updating: {error}
            </span>
          )}
        </div>
      </header>
      <div className="milestone-viewport">
        {!project ? (
          <p className="milestone-empty">Milestones will appear after this project is created.</p>
        ) : loaded ? (
          <MilestoneTimelineVisual milestones={milestones} />
        ) : error ? (
          // Only reached before the first answer; once there is a timeline to
          // show, a failure is reported by the header note instead.
          <p className="milestone-empty milestone-error">Could not load milestones: {error}</p>
        ) : (
          <p className="milestone-empty">Loading milestones…</p>
        )}
      </div>
    </section>
  );
}
