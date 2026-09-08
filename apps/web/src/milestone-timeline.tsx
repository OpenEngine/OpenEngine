import { useEffect, useMemo, useState, type SyntheticEvent } from "react";

import {
  getProjectMilestones,
  milestoneDetailsUrl,
  type ApiMilestone,
  type ApiProject,
} from "./api";

const NODE_GAP = 210;
const MIN_GRAPH_WIDTH = 640;
// Keep the first node at the same inset even when the map grows wider than the
// viewport. A proportional inset leaves the visible start of a long plan blank.
const MAP_SIDE_PADDING = 152;
const NODE_Y = 96;
// A node is out of flow, so the map cannot measure the stack inside it. These
// mirror `.milestone-node` in styles.css so the map can be told how far the
// deepest one reaches; without it a milestone's bullets hang past the map's
// floor and only the viewport's scrollbar admits they are there.
const NODE_TOP = 83;
const NODE_HEAD = 66; // dot 26 + gap 9 + one line of name 31
const NODE_ROW_GAP = 9;
const WORKSTREAM_LINE = 15; // 11px over 1.35, rounded up
const WORKSTREAM_GAP = 4;
const MAP_FLOOR = 180;
// Slack under the last bullet, which also absorbs a name that wraps to a
// second line -- the one part of the stack that cannot be counted from here.
const MAP_FOOT = 28;
// The gap a tooltip holds above what it describes, and the one it keeps from
// the window's edges -- the same 12px the graph's own side padding reserves.
const TOOLTIP_GAP = 8;
const TOOLTIP_GUTTER = 12;
const POLL_MS = 1000;
// One failed poll is a blip and is kept quiet; a run of them is an outage, and
// a timeline that has stopped following the plan has to say so.
const STALE_AFTER_FAILURES = 3;

/** How tall the map has to be for the deepest milestone to sit inside it. */
export function mapMinHeight(milestones: ApiMilestone[]): number {
  const rows = Math.max(0, ...milestones.map((milestone) => milestone.workstreams.length));
  const list = rows ? NODE_ROW_GAP + rows * WORKSTREAM_LINE + (rows - 1) * WORKSTREAM_GAP : 0;
  return Math.max(MAP_FLOOR, NODE_TOP + NODE_HEAD + list + MAP_FOOT);
}

/** Place a tooltip above the thing it describes, in the window's coordinates.
 *
 * The tooltip is fixed, so the map it hangs in can neither clip it nor scroll
 * it away, but that also means nothing places it: these two properties are the
 * whole of its position. The room it grows into is the chat above the
 * timeline, which is why it is put above the trigger rather than below it. */
function pinTooltip(event: SyntheticEvent<HTMLElement>) {
  const tooltip = event.currentTarget.querySelector<HTMLElement>(":scope > .milestone-tooltip");
  if (!tooltip) return;
  const trigger = event.currentTarget.getBoundingClientRect();
  // Hidden rather than unrendered, so it can be measured before it is shown:
  // its width is what decides whether it clears the window's edges.
  const half = tooltip.offsetWidth / 2;
  const left = Math.min(
    Math.max(trigger.left + trigger.width / 2, half + TOOLTIP_GUTTER),
    window.innerWidth - half - TOOLTIP_GUTTER,
  );
  tooltip.style.setProperty("--tooltip-left", `${left}px`);
  tooltip.style.setProperty(
    "--tooltip-bottom",
    `${window.innerHeight - trigger.top + TOOLTIP_GAP}px`,
  );
}

/** Whether two polls returned the same thing, compared as the server sent it. */
function same(a: unknown, b: unknown) {
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

export function MilestoneTimelineVisual({
  milestones,
  projectId,
}: {
  milestones: ApiMilestone[];
  /** The plan these belong to, which is what each milestone's link is built
   *  from: the graph is drawn beside a chat and on the project's own page, and
   *  both open the same milestone page. */
  projectId: string;
}) {
  const ordered = useMemo(() => orderMilestones(milestones), [milestones]);
  const positions = useMemo(
    () =>
      new Map(
        ordered.map((milestone, index) => [
          milestone.milestoneId,
          MAP_SIDE_PADDING + index * NODE_GAP,
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
    MAP_SIDE_PADDING * 2 + Math.max(0, ordered.length - 1) * NODE_GAP,
  );

  if (!ordered.length)
    return <p className="milestone-empty">No milestones have been added to this project yet.</p>;

  return (
    <div className="milestone-map" style={{ minWidth, minHeight: mapMinHeight(ordered) }}>
      <svg
        className="milestone-lines"
        aria-hidden="true"
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
        const nameId = `milestone-name-${milestone.milestoneId}`;
        return (
          <div
            key={milestone.milestoneId}
            className="milestone-node"
            style={{ left: positions.get(milestone.milestoneId) }}
            onMouseEnter={pinTooltip}
            onFocus={pinTooltip}
          >
            {milestone.description && (
              <span className="milestone-tooltip" id={tooltipId} role="tooltip">
                {milestone.description}
              </span>
            )}
            <a
              className="milestone-link"
              href={milestoneDetailsUrl(projectId, milestone.milestoneId)}
              aria-describedby={milestone.description ? tooltipId : undefined}
            >
              <span className="milestone-dot" aria-hidden="true" />
              <span className="milestone-name" id={nameId}>
                {milestone.name}
              </span>
            </a>
            {milestone.workstreams.length > 0 && (
              // Named off the milestone rather than by a string of its own:
              // two projects may hold two milestones called "Launch", and the
              // list belongs to the one written above it.
              <ul className="milestone-workstreams" aria-labelledby={nameId}>
                {milestone.workstreams.map((workstream) => {
                  const scopeId = `milestone-scope-${workstream.workstreamId}`;
                  return (
                    <li
                      key={workstream.workstreamId}
                      className="milestone-workstream"
                      tabIndex={workstream.scope ? 0 : undefined}
                      aria-describedby={workstream.scope ? scopeId : undefined}
                      onMouseEnter={pinTooltip}
                      onFocus={pinTooltip}
                    >
                      <span>{workstream.name}</span>
                      {workstream.scope && (
                        <span className="milestone-tooltip" id={scopeId} role="tooltip">
                          {workstream.scope}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

export type ProjectPlan = {
  /** The project the store answered with, for a page named after it. */
  project?: ApiProject;
  milestones: ApiMilestone[];
  /** Whether a first answer has arrived, which is what tells an empty plan
   *  from one that has not been read yet. */
  loaded: boolean;
  error: string;
  /** Whether this has stopped following the plan and is now only the last
   *  known one. */
  stale: boolean;
};

/** One project's plan, kept current.
 *
 *  Milestones are written by the planning tools in whatever process is running
 *  the agent, so a screen showing them re-reads the list rather than waiting to
 *  be told -- the same poll the shell already runs for projects and workflow
 *  runs. */
export function useProjectMilestones(projectId?: string): ProjectPlan {
  const [project, setProject] = useState<ApiProject>();
  const [milestones, setMilestones] = useState<ApiMilestone[]>([]);
  // Held apart from an empty list so a failed poll can keep showing the last
  // good plan rather than replace it with an error. It also gates the
  // previous project's plan on a switch, which is deliberately not cleared.
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [failures, setFailures] = useState(0);

  useEffect(() => {
    setLoaded(false);
    setError("");
    setFailures(0);
    if (!projectId) {
      setMilestones([]);
      setProject(undefined);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    const load = () => {
      void getProjectMilestones(projectId, controller.signal)
        .then((value) => {
          // Guarded like the two handlers below: whatever this poll answers
          // belongs to the project that asked for it, not the one now shown.
          if (controller.signal.aborted) return;
          // Keep the previous values when the plan has not moved, so the
          // ordering and position memos hold and a steady-state poll re-renders
          // nothing rather than redrawing the whole graph once a second.
          setMilestones((current) =>
            same(current, value.milestones) ? current : value.milestones,
          );
          setProject((current) => (same(current, value.project) ? current : value.project));
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
  }, [projectId]);

  return {
    project,
    milestones,
    loaded,
    error,
    // A plan that looks live but has stopped following the store is the worst
    // thing this can be, so a run of failures is said out loud beside a render
    // that is now only the last known plan.
    stale: loaded && failures >= STALE_AFTER_FAILURES,
  };
}

/** The timeline of the project this conversation is planning, kept current. */
export function MilestoneTimeline({
  project,
  collapsedUntilMilestone = false,
}: {
  project?: ApiProject;
  collapsedUntilMilestone?: boolean;
}) {
  const { milestones, loaded, error, stale } = useProjectMilestones(project?.projectId);
  const expanded = !collapsedUntilMilestone || milestones.length > 0;

  return (
    <section
      className={`milestone-timeline milestone-timeline-${expanded ? "expanded" : "collapsed"}`}
      aria-labelledby="milestone-timeline-title"
      aria-expanded={expanded}
    >
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
          <MilestoneTimelineVisual milestones={milestones} projectId={project.projectId} />
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
