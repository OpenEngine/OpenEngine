/** A project's plan on a page of its own.
 *
 *  The same timeline the planning conversation carries at its foot, given the
 *  room to be read: the graph at the top says how the goals depend on each
 *  other, and the two tables under it are where the work hanging off those
 *  goals will be read -- what is scheduled, and what is already finished. */

import { MilestoneTimelineVisual, useProjectMilestones } from "./milestone-timeline";

/** One half of the ledger under the graph.
 *
 *  The columns are settled; nothing reads workorders into them yet, so the
 *  table stands empty and says so rather than looking like a view of a store
 *  that happens to be bare. */
function WorkTable({ id, title, empty }: { id: string; title: string; empty: string }) {
  const titleId = `${id}-title`;
  return (
    <section className="work-table-section" aria-labelledby={titleId}>
      <h2 id={titleId}>{title}</h2>
      <table className="work-table" aria-labelledby={titleId}>
        <thead>
          <tr>
            <th scope="col">Workorder Name</th>
            <th scope="col">Workorder Description</th>
            <th scope="col">Milestone</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td className="work-table-empty" colSpan={3}>
              {empty}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

export function ProjectMilestonesPage({ projectId }: { projectId: string }) {
  const { project, milestones, loaded, error, stale } = useProjectMilestones(projectId);

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
            <MilestoneTimelineVisual milestones={milestones} projectId={projectId} />
          </section>
          <WorkTable
            id="scheduled-work"
            title="Scheduled Work"
            empty="No scheduled work yet."
          />
          <WorkTable
            id="finished-work"
            title="Finished Work"
            empty="No finished work yet."
          />
        </>
      )}
    </main>
  );
}
