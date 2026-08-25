/** A project's plan on a page of its own.
 *
 *  The same timeline the planning conversation carries at its foot, given the
 *  room to be read: the graph at the top says how the goals depend on each
 *  other, and a card apiece then says what each one actually is -- the
 *  description, the goals it waits on, and every workstream hanging from it,
 *  none of which fits in a 170px node. */

import { useMemo } from "react";

import type { ApiMilestone } from "./api";
import {
  MilestoneTimelineVisual,
  orderMilestones,
  useProjectMilestones,
} from "./milestone-timeline";

function MilestoneCard({
  milestone,
  names,
}: {
  milestone: ApiMilestone;
  /** Every milestone by id, so a dependency reads as the goal it names rather
   *  than as the id the planner recorded. */
  names: Map<string, string>;
}) {
  const dependencies = milestone.dependencies.map((id) => names.get(id) ?? id);
  const titleId = `milestone-card-${milestone.milestoneId}`;
  return (
    <article className="card milestone-card" aria-labelledby={titleId}>
      <div className="card-top">
        <span className="chip">
          {milestone.workstreams.length}{" "}
          {milestone.workstreams.length === 1 ? "workstream" : "workstreams"}
        </span>
        <code className="card-id">{milestone.milestoneId}</code>
      </div>
      <h2 id={titleId}>{milestone.name}</h2>
      {milestone.description && <p className="lede">{milestone.description}</p>}
      {dependencies.length > 0 && (
        <p className="micro">Depends on {dependencies.join(" · ")}</p>
      )}
      {milestone.workstreams.length > 0 ? (
        // Named in full rather than off the heading above it: the timeline on
        // the same page already labels a list with this milestone's bare name,
        // and two lists answering to "Foundation" name neither of them.
        <ul
          className="milestone-card-workstreams"
          aria-label={`Workstreams for ${milestone.name}`}
        >
          {milestone.workstreams.map((workstream) => (
            <li key={workstream.workstreamId}>
              <strong>{workstream.name}</strong>
              {workstream.scope && <span>{workstream.scope}</span>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="micro">No workstreams yet.</p>
      )}
    </article>
  );
}

export function ProjectMilestonesPage({ projectId }: { projectId: string }) {
  const { project, milestones, loaded, error, stale } = useProjectMilestones(projectId);
  // The cards read in the order the timeline draws, so the page is one plan
  // told twice rather than two orderings of it.
  const ordered = useMemo(() => orderMilestones(milestones), [milestones]);
  const names = useMemo(
    () => new Map(milestones.map((milestone) => [milestone.milestoneId, milestone.name])),
    [milestones],
  );

  return (
    <main className="panel-scroll">
      <header className="detail-head">
        {project?.conversationUrl && (
          <a className="back-link" href={project.conversationUrl}>
            ← Planning conversation
          </a>
        )}
        <div className="detail-title">
          <div>
            <p className="eyebrow">{project?.name ?? "Project"}</p>
            <h1>Milestones</h1>
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
          // Only reached before the first answer; once there is a plan on
          // screen, a failure is reported by the header note instead.
          <p className="notice notice-block">Could not load milestones: {error}</p>
        ) : (
          <p className="state-inline">Loading milestones…</p>
        )
      ) : (
        <>
          <section
            className="milestone-viewport milestone-page-map"
            aria-label="Milestone timeline"
          >
            <MilestoneTimelineVisual milestones={milestones} />
          </section>
          {ordered.length > 0 && (
            <div className="cards">
              {ordered.map((milestone) => (
                <MilestoneCard
                  key={milestone.milestoneId}
                  milestone={milestone}
                  names={names}
                />
              ))}
            </div>
          )}
        </>
      )}
    </main>
  );
}
