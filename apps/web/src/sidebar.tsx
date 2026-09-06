/** The rail: Projects and WorkOrders, in that order, with at most one open.
 *
 *  Both headers stay on screen at all times and the open one takes the
 *  space that is left, so choosing a section slides the headers above it up
 *  and the ones below it down rather than swapping one rail for another. The
 *  open header is printed in white and the closed ones in the rail's muted
 *  ink, which is the whole of the selected state. Clicking the open header
 *  again closes it, leaving the two headers stacked and nothing beneath
 *  them. */

import { useState, type ReactNode } from "react";

import {
  graphConversationUrl,
  projectMilestonesUrl,
  type ApiGraphTopology,
  type ApiProject,
  type ApiWorkflowRunListing,
} from "./api";
import { RailBrand, RailFoot } from "./brand";
import { SettingsPanel } from "./settings-panel";
import { isGraphRun, IN_PROGRESS_PHASES, runStatusLabel } from "./runs";

export type RailSection = "projects" | "workflows";

/** The nodes of each graph, by the id of the graph they belong to. */
export type GraphNodes = Record<string, ApiGraphTopology["nodes"]>;

/** One shortcut under a WorkOrder's name: a conversation it holds. */
type RailConversation = {
  key: string;
  name: string;
  href: string;
  waiting: boolean;
};

/** What one WorkOrder offers beneath its name.
 *
 *  A step run offers the conversations its steps have started, named after the
 *  step that owns each. A `[BETA]` run has no steps: its stages are its graph's
 *  nodes, so those are what it offers, under their own names and from the moment
 *  the run exists rather than once an agent has said something. A node that says
 *  it is not one of the run's conversations -- the checkout, the person's own
 *  verdict -- is left out; see `show_in_sidebar` on `GraphNode`.
 *
 *  A graph whose nodes have not been read yet offers nothing, which is the rail
 *  as it read before they were offered at all. */
function conversationsOf(
  run: ApiWorkflowRunListing,
  nodes: GraphNodes,
): RailConversation[] {
  if (isGraphRun(run))
    return (nodes[run.workflowId] ?? [])
      .filter((node) => node.showInSidebar !== false)
      .map((node) => ({
        key: node.nodeId,
        name: node.name,
        href: graphConversationUrl(run.runId, node.nodeId),
        waiting: false,
      }));
  return run.steps
    .filter((step) => step.conversationUrl)
    .map((step) => ({
      key: step.stepId,
      name: `${step.name} conversation`,
      href: step.conversationUrl!,
      waiting: step.waiting,
    }));
}

function Section({
  id,
  title,
  open,
  onToggle,
  children,
}: {
  id: RailSection;
  title: string;
  open: boolean;
  onToggle: (section: RailSection) => void;
  children: ReactNode;
}) {
  return (
    <section className="rail-section" data-open={open || undefined}>
      <button
        aria-controls={`rail-${id}`}
        aria-expanded={open}
        className="rail-head"
        onClick={() => onToggle(id)}
        type="button"
      >
        {title}
      </button>
      {/* A closed section is laid out at zero height rather than unmounted, so
          the slide has something to move; `inert` is what keeps the Tab key and
          screen readers out of the part of it that is off screen. */}
      <div className="rail-section-body" id={`rail-${id}`} inert={!open}>
        {children}
      </div>
    </section>
  );
}

/** One project in the rail, with the click that puts it away or brings it back.
 *
 *  An archived one is the plain row a project with no conversation already
 *  gets: it is not what you are reading, and restoring is the thing to do with
 *  it. That is how an archived chat reads too. */
function ProjectItem({
  project,
  active,
  insideProject = false,
  showingMilestones = false,
  onArchive,
}: {
  project: ApiProject;
  active: boolean;
  /** Whether any page belonging to this project is on screen. */
  insideProject?: boolean;
  /** Whether this project's milestones are the page on screen. */
  showingMilestones?: boolean;
  onArchive?: (project: ApiProject, archived: boolean) => void;
}) {
  const copy = (
    <span className="rail-item-title" data-clamp="">
      {project.name}
    </span>
  );
  // A project that has planned nothing has no page of milestones to offer, and
  // an archived one has been put away along with its plan -- the same reason
  // its row stops being a link to the conversation.
  const milestones = !project.archived && (project.milestoneCount ?? 0) > 0;
  return (
    <div className="rail-group">
      <div className="rail-item" data-active={active || insideProject || undefined}>
        {project.conversationUrl && !project.archived ? (
          <a
            aria-current={active ? "page" : undefined}
            className="rail-item-trigger"
            href={project.conversationUrl}
          >
            {copy}
          </a>
        ) : (
          <div className="rail-item-trigger">{copy}</div>
        )}
        {onArchive &&
          (project.archived ? (
            <button
              aria-label={`Restore ${project.name}`}
              className="rail-item-action"
              onClick={() => onArchive(project, false)}
              title="Restore project"
              type="button"
            >
              Restore
            </button>
          ) : (
            <button
              aria-label={`Archive ${project.name}`}
              className="rail-item-action"
              onClick={() => onArchive(project, true)}
              title="Archive project"
              type="button"
            >
              ×
            </button>
          ))}
      </div>
      {milestones && (
        <div className="rail-sub" aria-label={`Milestones for ${project.name}`}>
          <a
            aria-current={showingMilestones ? "page" : undefined}
            data-active={showingMilestones || undefined}
            href={projectMilestonesUrl(project.projectId)}
          >
            Milestones · {project.milestoneCount}
          </a>
        </div>
      )}
    </div>
  );
}

export function Sidebar({
  projects = [],
  runs,
  graphNodes = {},
  initialSection,
  activeRunId,
  activeConversationUrl,
  activeProjectId,
  activeMilestonesPage = true,
  activeView,
  onArchiveProject,
  onDeleteRun,
}: {
  projects?: ApiProject[];
  runs: ApiWorkflowRunListing[];
  /** The graphs behind the `[BETA]` WorkOrders listed, which is where their
   *  conversations are named. Empty until they have been read, and for a rail
   *  whose owner does not follow them. */
  graphNodes?: GraphNodes;
  /** Which section the page on screen belongs to, followed until the reader
   *  opens one themselves. */
  initialSection: RailSection;
  activeRunId?: string;
  activeConversationUrl?: string;
  /** The project whose milestones are on screen, when that is the page. */
  activeProjectId?: string;
  /** Whether the project page on screen is the milestones index itself. A
   *  milestone child keeps the project row marked but is not the page named by
   *  its parent link. Defaults true for callers predating child pages. */
  activeMilestonesPage?: boolean;
  activeView?: "runs" | "new";
  /** Omitted where nothing owns the projects list, which leaves the rows
   *  readable and drops a button that could not have worked. */
  onArchiveProject?: (project: ApiProject, archived: boolean) => void;
  /** Omitted for the same reason `onArchiveProject` is: a rail nobody owns the
   *  runs list for is left readable rather than given a button that cannot
   *  remove anything from it. */
  onDeleteRun?: (run: ApiWorkflowRunListing) => void;
}) {
  // Where the page belongs is not always known at the first paint: a
  // conversation is only recognized as a project's once the projects load. The
  // rail follows that until the reader opens a section themselves, after which
  // it is their choice on screen and nothing else moves it. Closing the open
  // one is such a choice, and `closed` is how it is held: not "nothing chosen
  // yet", which is what would put the page's section back on screen.
  const [chosen, setChosen] = useState<RailSection | "closed" | null>(null);
  const open = chosen === null ? initialSection : chosen === "closed" ? null : chosen;
  const toggle = (section: RailSection) => setChosen(section === open ? "closed" : section);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // A row with nowhere to go is never the page you are reading. Both sides are
  // optional here, so comparing them alone would call two absent URLs a match
  // and mark every such row on every page.
  const isActive = (project: ApiProject) =>
    project.conversationUrl !== undefined &&
    project.conversationUrl === activeConversationUrl;
  const archivedProjects = projects.filter((project) => project.archived);
  return (
    <aside className="rail">
      <RailBrand href="/" />
      <div className="rail-sections">
        <Section id="projects" title="Projects" open={open === "projects"} onToggle={toggle}>
          <div className="rail-nav">
            <a className="rail-button rail-button-primary" href="/plan">
              + New project
            </a>
          </div>
          {/* A project opens the planning conversation it was named after,
              which is the only page it has so far. One without a conversation
              is still listed -- it says the project exists -- but there is
              nowhere to send a click, so it stays the plain row it reads as. */}
          <nav className="rail-scroll" aria-label="Projects">
            {projects
              .filter((project) => !project.archived)
              .map((project) => (
                <ProjectItem
                  active={isActive(project)}
                  insideProject={activeProjectId === project.projectId}
                  key={project.projectId}
                  onArchive={onArchiveProject}
                  project={project}
                  showingMilestones={
                    activeMilestonesPage && activeProjectId === project.projectId
                  }
                />
              ))}
            {archivedProjects.length > 0 && (
              <details className="rail-archive">
                <summary>Archived projects</summary>
                {archivedProjects.map((project) => (
                  <ProjectItem
                    active={false}
                    key={project.projectId}
                    onArchive={onArchiveProject}
                    project={project}
                  />
                ))}
              </details>
            )}
          </nav>
        </Section>
        <Section id="workflows" title="WorkOrders" open={open === "workflows"} onToggle={toggle}>
          <div className="rail-nav">
            <a className="rail-button rail-button-primary" href="/runs/new">
              + New WorkOrder
            </a>
            <a
              className="rail-button"
              data-active={activeView === "runs" || undefined}
              href="/runs"
            >
              All WorkOrders
            </a>
          </div>
          <nav className="rail-scroll" aria-label="Recent WorkOrders">
            {runs.map((run) => {
              const conversations = conversationsOf(run, graphNodes);
              return (
                <div className="rail-group" key={run.runId}>
                  <div
                    className="rail-item"
                    data-active={
                      activeRunId === run.runId && !activeConversationUrl ? true : undefined
                    }
                  >
                    <a className="rail-item-trigger" href={`/runs/${run.runId}`}>
                      <span className="rail-item-title" data-clamp="">
                        {run.name}
                      </span>
                      <span className="rail-item-meta">
                        {IN_PROGRESS_PHASES.has(run.phase) && (
                          <span className="rail-live" aria-label="WorkOrder is in progress" />
                        )}
                        {runStatusLabel(run)} · {run.workflowVersion || run.workflowId}
                      </span>
                    </a>
                    {/* The project row's × put next to a WorkOrder, where it
                        throws the run away rather than putting it aside: a run
                        has no archived list to sit in, so the click is asked
                        about before it is made. */}
                    {onDeleteRun && (
                      <button
                        aria-label={`Delete ${run.name}`}
                        className="rail-item-action"
                        onClick={() => onDeleteRun(run)}
                        title="Delete WorkOrder"
                        type="button"
                      >
                        ×
                      </button>
                    )}
                  </div>
                  {conversations.length > 0 && (
                    <div className="rail-sub" aria-label={`Conversations for ${run.name}`}>
                      {conversations.map((conversation) => (
                        <a
                          aria-current={
                            activeConversationUrl === conversation.href ? "page" : undefined
                          }
                          data-active={
                            activeConversationUrl === conversation.href || undefined
                          }
                          href={conversation.href}
                          key={conversation.key}
                        >
                          {conversation.name}
                          {conversation.waiting && (
                            <span aria-label="Waiting for input"> ❔</span>
                          )}
                        </a>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </nav>
        </Section>
      </div>
      {settingsOpen ? (
        <SettingsPanel onClose={() => setSettingsOpen(false)} />
      ) : (
        <RailFoot onSettings={() => setSettingsOpen(true)} />
      )}
    </aside>
  );
}
