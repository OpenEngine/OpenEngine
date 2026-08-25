/** The rail: Projects, Workflows, Chats, in that order, with at most one open.
 *
 *  All three headers stay on screen at all times and the open one takes the
 *  space that is left, so choosing a section slides the headers above it up
 *  and the ones below it down rather than swapping one rail for another. The
 *  open header is printed in white and the closed ones in the rail's muted
 *  ink, which is the whole of the selected state. Clicking the open header
 *  again closes it, leaving the three headers stacked and nothing beneath
 *  them. */

import {
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  useAuiState,
} from "@assistant-ui/react";
import { useState, type ReactNode } from "react";

import { projectMilestonesUrl, type ApiProject, type ApiWorkflowRun } from "./api";
import { RailBrand, RailFoot } from "./brand";
import { conversationCount, IN_PROGRESS_PHASES, runStatusLabel } from "./runs";

export type RailSection = "projects" | "workflows" | "chats";

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

function ThreadItemMeta() {
  const custom = useAuiState((state) => state.threadListItem.custom) as
    | { agentId?: string; runner?: string; workspaceRoot?: string }
    | undefined;
  const isRunning = useAuiState((state) => state.threadListItem.isRunning);
  return (
    <span className="rail-item-meta">
      {isRunning && <span className="rail-live" aria-label="Agent is running" />}
      {[custom?.agentId, custom?.runner].filter(Boolean).join(" · ")}
    </span>
  );
}

/** One chat in the rail.
 *
 *  `linked` is for the rails beside a workflow page, where switching the
 *  runtime's conversation would change nothing anybody can see: there, a chat
 *  is a link to its own page instead. */
function ThreadListItem({
  archived = false,
  linked = false,
}: {
  archived?: boolean;
  linked?: boolean;
}) {
  const remoteId = useAuiState((state) => state.threadListItem.remoteId);
  const copy = (
    <>
      <span className="rail-item-title" data-clamp="">
        <ThreadListItemPrimitive.Title fallback="New chat" />
      </span>
      <ThreadItemMeta />
    </>
  );
  return (
    <ThreadListItemPrimitive.Root className="rail-item">
      {archived ? (
        <div className="rail-item-trigger">{copy}</div>
      ) : linked && remoteId ? (
        <a
          className="rail-item-trigger"
          href={`/conversations/${encodeURIComponent(remoteId)}`}
        >
          {copy}
        </a>
      ) : (
        <ThreadListItemPrimitive.Trigger className="rail-item-trigger">
          {copy}
        </ThreadListItemPrimitive.Trigger>
      )}
      {archived ? (
        <ThreadListItemPrimitive.Unarchive
          className="rail-item-action"
          aria-label="Restore chat"
          title="Restore chat"
        >
          Restore
        </ThreadListItemPrimitive.Unarchive>
      ) : (
        <ThreadListItemPrimitive.Archive
          className="rail-item-action"
          aria-label="Archive chat"
          title="Archive chat"
        >
          ×
        </ThreadListItemPrimitive.Archive>
      )}
    </ThreadListItemPrimitive.Root>
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
  showingMilestones = false,
  onArchive,
}: {
  project: ApiProject;
  active: boolean;
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
    <div>
      <div className="rail-item" data-active={active || undefined}>
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
  initialSection,
  linkChats = false,
  activeRunId,
  activeConversationUrl,
  activeProjectId,
  activeView,
  onArchiveProject,
}: {
  projects?: ApiProject[];
  runs: ApiWorkflowRun[];
  /** Which section the page on screen belongs to, followed until the reader
   *  opens one themselves. */
  initialSection: RailSection;
  linkChats?: boolean;
  activeRunId?: string;
  activeConversationUrl?: string;
  /** The project whose milestones are on screen, when that is the page. */
  activeProjectId?: string;
  activeView?: "runs" | "new";
  /** Omitted where nothing owns the projects list, which leaves the rows
   *  readable and drops a button that could not have worked. */
  onArchiveProject?: (project: ApiProject, archived: boolean) => void;
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
                  key={project.projectId}
                  onArchive={onArchiveProject}
                  project={project}
                  showingMilestones={activeProjectId === project.projectId}
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
        <Section id="workflows" title="Workflows" open={open === "workflows"} onToggle={toggle}>
          <div className="rail-nav">
            <a className="rail-button rail-button-primary" href="/runs/new">
              + New workflow
            </a>
            <a
              className="rail-button"
              data-active={activeView === "runs" || undefined}
              href="/runs"
            >
              All workflow runs
            </a>
          </div>
          <nav className="rail-scroll" aria-label="Recent runs">
            {runs.map((run) => (
              <div key={run.runId}>
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
                        <span className="rail-live" aria-label="Workflow is in progress" />
                      )}
                      {runStatusLabel(run)} · {run.workflowVersion || run.workflowId}
                    </span>
                  </a>
                </div>
                {conversationCount(run) > 0 && (
                  <div className="rail-sub" aria-label={`Conversations for ${run.name}`}>
                    {run.steps
                      .filter((step) => step.conversationUrl)
                      .map((step) => (
                        <a
                          aria-current={
                            activeConversationUrl === step.conversationUrl ? "page" : undefined
                          }
                          data-active={activeConversationUrl === step.conversationUrl || undefined}
                          href={step.conversationUrl!}
                          key={step.stepId}
                        >
                          {step.name} conversation
                          {step.waiting && <span aria-label="Waiting for input"> ❔</span>}
                        </a>
                      ))}
                  </div>
                )}
              </div>
            ))}
          </nav>
        </Section>
        <Section id="chats" title="Chats" open={open === "chats"} onToggle={toggle}>
          <ThreadListPrimitive.Root className="rail-list">
            <div className="rail-nav">
              {linkChats ? (
                <a className="rail-button rail-button-primary" href="/conversations">
                  + New chat
                </a>
              ) : (
                <ThreadListPrimitive.New className="rail-button rail-button-primary">
                  + New chat
                </ThreadListPrimitive.New>
              )}
            </div>
            <div className="rail-scroll">
              <ThreadListPrimitive.Items>
                {() => <ThreadListItem linked={linkChats} />}
              </ThreadListPrimitive.Items>
              <details className="rail-archive">
                <summary>Archived</summary>
                <ThreadListPrimitive.Items archived>
                  {() => <ThreadListItem archived />}
                </ThreadListPrimitive.Items>
              </details>
            </div>
          </ThreadListPrimitive.Root>
        </Section>
      </div>
      <RailFoot />
    </aside>
  );
}
