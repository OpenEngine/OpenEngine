/** WorkOrders and their conversations. */

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

import {
  graphConversationUrl,
  type ApiGraphTopology,
  type ApiWorkflowRunListing,
} from "./api";
import { RailBrand, RailFoot } from "./brand";
import { LoopList, LoopSettingsForm } from "./loops";
import { SettingsPanel } from "./settings-panel";
import { runArchived, runFinished, runStatusLabel } from "./runs";

export type RailSection = "workflows" | "loops";

/** The nodes of each graph, by the id of the graph they belong to. */
export type GraphNodes = Record<string, ApiGraphTopology["nodes"]>;

/** One shortcut under a WorkOrder's name: a conversation it holds. */
type RailConversation = {
  key: string;
  name: string;
  href: string;
  waiting: boolean;
  group?: string;
};

type RailConversationGroup = {
  key: string;
  name: string;
  conversations: RailConversation[];
};

/** Keep ungrouped conversations in place and replace each named group with one
 *  collapsible entry at the position of its first member. A group holding only
 *  one conversation is shown as that conversation, with nothing to unfold. */
function groupConversations(
  conversations: RailConversation[],
): (RailConversation | RailConversationGroup)[] {
  const groups = new Map<string, RailConversationGroup>();
  const entries: (RailConversation | RailConversationGroup)[] = [];
  for (const conversation of conversations) {
    if (!conversation.group) {
      entries.push(conversation);
      continue;
    }
    const existing = groups.get(conversation.group);
    if (existing) {
      existing.conversations.push(conversation);
      continue;
    }
    const group = {
      key: `group-${conversation.group}`,
      name: conversation.group,
      conversations: [conversation],
    };
    groups.set(conversation.group, group);
    entries.push(group);
  }
  return entries.map((entry) =>
    "conversations" in entry && entry.conversations.length === 1
      ? entry.conversations[0]
      : entry,
  );
}

function ConversationLink({ conversation, activeUrl }: {
  conversation: RailConversation;
  activeUrl?: string;
}) {
  const active = activeUrl === conversation.href;
  return (
    <a
      aria-current={active ? "page" : undefined}
      data-active={active || undefined}
      href={conversation.href}
    >
      {conversation.name}
      {conversation.waiting && <span aria-label="Waiting for input"> ❔</span>}
    </a>
  );
}

/** What one WorkOrder offers beneath its name.
 *
 *  A step run offers the conversations its steps have started, named after the
 *  step that owns each. A graph run has no steps: its stages are its graph's
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
  return (nodes[run.workflowId] ?? [])
    .filter((node) => node.showInSidebar !== false)
    .map((node) => ({
      key: node.nodeId,
      name: node.name,
      href: graphConversationUrl(run.runId, node.nodeId),
      group: node.group || undefined,
      waiting: !runFinished(run) &&
        (run.graphProgress?.waitingNodeIds.includes(node.nodeId) ?? false),
    }));
}

function currentGraphNodes(run: ApiWorkflowRunListing) {
  const progress = runFinished(run) ? undefined : run.graphProgress;
  return progress ? [...new Set([
    ...progress.activeNodeIds,
    ...progress.waitingNodeIds,
    ...(progress.activeNodeIds.length || progress.waitingNodeIds.length ? [] : progress.nextNodeIds),
  ])] : [];
}

function workOrderCategories(run: ApiWorkflowRunListing, nodes: GraphNodes): string[] {
  if (runFinished(run)) return [run.phase];
  const stages = currentGraphNodes(run).map((id) =>
    nodes[run.workflowId]?.find((node) => node.nodeId === id)?.name ?? id,
  );
  return stages.length ? stages : [runStatusLabel(run)];
}

function WorkOrderFilters({ options, excluded, onChange, archived, onArchivedChange }: {
  options: string[];
  excluded: string[];
  onChange: (excluded: string[]) => void;
  /** Whether the done WorkOrders are in the rail. */
  archived: boolean;
  onArchivedChange: (archived: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!open || !menu) return;
    const constrainHeight = () => {
      const bounds = menu.getBoundingClientRect();
      const opensUp = getComputedStyle(menu).getPropertyValue("--filter-opens-up").trim() === "1";
      const available = opensUp ? bounds.bottom : window.innerHeight - bounds.top;
      menu.style.maxHeight = `${Math.max(0, available - 8)}px`;
    };
    constrainHeight();
    window.addEventListener("resize", constrainHeight);
    return () => window.removeEventListener("resize", constrainHeight);
  }, [open]);
  return (
    <div className="rail-filter" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }} onKeyDown={(event) => {
      if (event.key === "Escape") {
        setOpen(false);
        event.currentTarget.querySelector("button")?.focus();
      }
    }}>
      <button type="button" className="rail-filter-toggle" aria-label="Filter WorkOrders"
        title="Filter WorkOrders" aria-expanded={open} aria-controls="rail-workorder-filters"
        data-active={
          archived || options.some((option) => excluded.includes(option)) || undefined
        }
        onClick={() => setOpen(!open)}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 4h18l-7 8v7l-4 2v-9z" />
        </svg>
      </button>
      {open && <div ref={menuRef} className="rail-filter-options" id="rail-workorder-filters"
        role="group" aria-label="WorkOrder filters">
        {/* Above the stages, and apart from them: the stages narrow what is
            listed, while this is what decides which of them are there to
            narrow at all. An archived WorkOrder is in no stage any more. */}
        <label className="rail-filter-archive">
          <input type="checkbox" checked={archived} onChange={(event) =>
            onArchivedChange(event.target.checked)
          } />
          Archived
        </label>
        {options.length === 0 && <span>No WorkOrder history yet.</span>}
        {options.map((filter) => <label key={filter}>
          <input type="checkbox" checked={!excluded.includes(filter)} onChange={(event) =>
            onChange(event.target.checked ? excluded.filter((item) => item !== filter) : [...excluded, filter])
          } />
          {filter}
        </label>)}
      </div>}
    </div>
  );
}

function Section({
  id,
  title,
  open,
  onToggle,
  children,
  action,
}: {
  id: RailSection;
  title: string;
  open: boolean;
  onToggle: (section: RailSection) => void;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section className="rail-section" data-open={open || undefined}>
      <div className="rail-heading">
        <button
          aria-controls={`rail-${id}`}
          aria-expanded={open}
          className="rail-head"
          onClick={() => onToggle(id)}
          type="button"
        >
          {title}
        </button>
        {action}
      </div>
      {/* A closed section is laid out at zero height rather than unmounted, so
          the slide has something to move; `inert` is what keeps the Tab key and
          screen readers out of the part of it that is off screen. */}
      <div className="rail-section-body" id={`rail-${id}`} inert={!open}>
        {children}
      </div>
    </section>
  );
}

/** WorkOrders, their conversations, and the controls for filtering them. */
export function Sidebar({
  runs,
  graphNodes = {},
  runners = [],
  initialSection = "workflows",
  activeRunId,
  activeConversationUrl,
  activeView,
  activeLoopId,
  onDeleteRun,
}: {
  runs: ApiWorkflowRunListing[];
  /** The graphs behind the graph WorkOrders listed, which is where their
   *  conversations are named. Empty until they have been read, and for a rail
   *  whose owner does not follow them. */
  graphNodes?: GraphNodes;
  /** The runners a loop's manual strategy can choose between. */
  runners?: string[];
  /** Which section the page on screen belongs to, followed until the reader
   *  opens one themselves. */
  initialSection?: RailSection;
  activeRunId?: string;
  activeConversationUrl?: string;
  activeView?: "runs" | "new" | "new-loop" | "utilization";
  /** The loop whose page is on screen. */
  activeLoopId?: string;
  /** Remove a run from the list. */
  onDeleteRun?: (run: ApiWorkflowRunListing) => void;
}) {
  const [chosen, setChosen] = useState<RailSection | "closed" | null>(null);
  const selected = chosen === null ? initialSection : chosen === "closed" ? null : chosen;
  const open = selected;
  const toggle = (section: RailSection) => setChosen(section === open ? "closed" : section);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // The loop settings are read the first time the section opens, not with
  // every rail, and kept mounted after so closing it does not lose an edit.
  const [loopsOpened, setLoopsOpened] = useState(false);
  if (open === "loops" && !loopsOpened) setLoopsOpened(true);
  // Keep exclusions so newly observed stages are selected without resetting user choices.
  const [excludedFilters, setExcludedFilters] = useState<string[]>([]);
  // The archive is out of the rail until it is asked for. A WorkOrder a person
  // has accepted is done, and the rail is what is being worked on -- the list
  // would otherwise grow by one every time a run is finished with and never
  // shrink.
  const [showArchived, setShowArchived] = useState(false);
  runs = runs.filter((run) => run.phase !== "scheduled");
  const listed = showArchived ? runs : runs.filter((run) => !runArchived(run));
  const runCategories = listed.map((run) => workOrderCategories(run, graphNodes));
  const filterOptions = [...new Set(runCategories.flat())].sort((a, b) => a.localeCompare(b));
  const filteredRuns = listed.filter((_, index) =>
    runCategories[index].some((category) => !excludedFilters.includes(category)),
  );
  return (
    <aside className="rail">
      <RailBrand href="/" />
      <div className="rail-sections">
        <Section id="workflows" title="WorkOrders" open={open === "workflows"} onToggle={toggle}
          action={<WorkOrderFilters options={filterOptions} excluded={excludedFilters}
            onChange={setExcludedFilters} archived={showArchived}
            onArchivedChange={setShowArchived} />}>
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
            {runs.length > 0 && filteredRuns.length === 0 && (
              <p className="rail-note">No WorkOrders match the selected filters.</p>
            )}
            {filteredRuns.map((run) => {
              const conversations = conversationsOf(run, graphNodes);
              const conversationGroups = groupConversations(conversations);
              const progress = runFinished(run) ? undefined : run.graphProgress;
              const currentNodes = currentGraphNodes(run);
              const status = [...new Set(currentNodes.map((id) => {
                const node = graphNodes[run.workflowId]
                  ?.find((candidate) => candidate.nodeId === id);
                return node?.group || node?.name || id;
              }))].join(", ") || runStatusLabel(run);
              const executing = !!progress?.activeNodeIds.length;
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
                        {executing && (
                          <span className="rail-live" aria-label="WorkOrder is in progress" />
                        )}
                        {status} · {run.workflowId}
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
                      {conversationGroups.map((entry) =>
                        "conversations" in entry ? (
                          <details
                            className="rail-sub-group"
                            open={entry.conversations.some(
                              (conversation) => activeConversationUrl === conversation.href,
                            ) || undefined}
                            key={entry.key}
                          >
                            <summary>
                              {entry.name}
                              {entry.conversations.some((conversation) => conversation.waiting) && (
                                <span aria-label="Waiting for input"> ❔</span>
                              )}
                            </summary>
                            <div>
                              {entry.conversations.map((conversation) => (
                                <ConversationLink
                                  activeUrl={activeConversationUrl}
                                  conversation={conversation}
                                  key={conversation.key}
                                />
                              ))}
                            </div>
                          </details>
                        ) : (
                          <ConversationLink
                            activeUrl={activeConversationUrl}
                            conversation={entry}
                            key={entry.key}
                          />
                        ),
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </nav>
        </Section>
        <Section id="loops" title="Loops" open={open === "loops"} onToggle={toggle}>
          <div className="rail-nav">
            <a className="rail-button rail-button-primary" href="/loops/new"
              data-active={activeView === "new-loop" || undefined}>
              + New Loop
            </a>
          </div>
          {loopsOpened && <LoopList activeLoopId={activeLoopId} />}
          {loopsOpened && <LoopSettingsForm runners={runners} />}
        </Section>
      </div>
      {settingsOpen ? (
        <SettingsPanel onClose={() => setSettingsOpen(false)} />
      ) : (
        <RailFoot
          onSettings={() => setSettingsOpen(true)}
          utilizationActive={activeView === "utilization"}
        />
      )}
    </aside>
  );
}
