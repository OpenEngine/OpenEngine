import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ApiWorkflowRun } from "./api";
import { Sidebar } from "./sidebar";

/** The rail's chat list is assistant-ui's, and mounting the real runtime would
 *  test their thread store rather than our rail. One chat, standing in. */
vi.mock("@assistant-ui/react", () => {
  const thread = {
    remoteId: "thread-1",
    custom: { agentId: "coder", runner: "claude-code" },
    isRunning: false,
  };
  const Div = ({ children, ...props }: ComponentProps<"div">) => (
    <div {...props}>{children}</div>
  );
  const Button = ({ children, ...props }: ComponentProps<"button">) => (
    <button type="button" {...props}>
      {children}
    </button>
  );
  return {
    useAuiState: (select: (state: { threadListItem: typeof thread }) => unknown) =>
      select({ threadListItem: thread }),
    ThreadListPrimitive: {
      Root: Div,
      New: Button,
      Items: ({ archived, children }: { archived?: boolean; children: () => ReactNode }) =>
        archived ? null : children(),
    },
    ThreadListItemPrimitive: {
      Root: Div,
      Trigger: Button,
      Title: () => <>First chat</>,
      Archive: Button,
      Unarchive: Button,
    },
  };
});

const run: ApiWorkflowRun = {
  runId: "run-1",
  name: "First run",
  workflowId: "work-v1",
  workflowName: "Work",
  workflowVersion: "v1",
  taskId: "task-1",
  workstreamId: null,
  milestoneId: null,
  taskPrompt: "Do the work",
  repository: ".",
  repositoryContext: { repository: "." },
  phase: "running_agent",
  currentStepId: "implement",
  terminalOutcome: null,
  failureReason: "",
  steps: [
    {
      stepId: "implement",
      name: "Implementation",
      kind: "agent",
      status: "in_progress",
      outcome: null,
      summary: "",
      outputs: [],
      changesRequested: false,
      agentId: "agent",
      agentInstanceId: "instance",
      agentRunId: "agent-run",
      conversationId: "conversation",
      conversationUrl: "/runs/run-1/conversations/conversation",
      waiting: false,
    },
  ],
  pendingHumanReview: null,
  humanDecision: null,
};

function header(name: string) {
  return screen.getByRole("button", { name });
}

/** The part of a section a header controls, open or not, which is where that
 *  section's own buttons and items are. */
function body(name: string) {
  const id = header(name).getAttribute("aria-controls") as string;
  return document.getElementById(id) as HTMLElement;
}

describe("Sidebar", () => {
  it("keeps the three sections in one order and opens only the one asked for", () => {
    const { container } = render(<Sidebar runs={[run]} initialSection="workflows" />);

    const headers = [...container.querySelectorAll("[aria-expanded]")];
    expect(headers.map((element) => element.textContent)).toEqual([
      "Projects",
      "WorkOrders",
      "Chats",
    ]);
    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "true");
    expect(header("Projects")).toHaveAttribute("aria-expanded", "false");
    expect(header("Chats")).toHaveAttribute("aria-expanded", "false");
  });

  it("moves the open state to the clicked section and closes the others", async () => {
    const user = userEvent.setup();
    render(<Sidebar runs={[run]} initialSection="workflows" />);

    await user.click(header("Chats"));

    expect(header("Chats")).toHaveAttribute("aria-expanded", "true");
    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "false");

    await user.click(header("Projects"));

    expect(header("Projects")).toHaveAttribute("aria-expanded", "true");
    expect(header("Chats")).toHaveAttribute("aria-expanded", "false");
    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "false");
  });

  /** Which section a conversation belongs to is only known once the projects
   *  load, so the rail follows a late answer -- but never over the reader. */
  it("follows a late section until the reader opens one themselves", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Sidebar runs={[run]} initialSection="chats" />);

    rerender(<Sidebar runs={[run]} initialSection="projects" />);
    expect(header("Projects")).toHaveAttribute("aria-expanded", "true");

    await user.click(header("Chats"));
    rerender(<Sidebar runs={[run]} initialSection="workflows" />);

    expect(header("Chats")).toHaveAttribute("aria-expanded", "true");
    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "false");
  });

  /** The header is the whole control, so the way back out of a section is the
   *  way in: the rail can sit with all three headers stacked and nothing open.
   *  Closing is the reader's choice like any other, so a late answer about
   *  where the page belongs does not fold the rail back open. */
  it("closes the open section when its own header is clicked again", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Sidebar runs={[run]} initialSection="workflows" />);

    await user.click(header("WorkOrders"));

    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "false");
    expect(body("WorkOrders")).toHaveAttribute("inert");
    expect(header("Projects")).toHaveAttribute("aria-expanded", "false");
    expect(header("Chats")).toHaveAttribute("aria-expanded", "false");

    rerender(<Sidebar runs={[run]} initialSection="projects" />);
    expect(header("Projects")).toHaveAttribute("aria-expanded", "false");

    await user.click(header("WorkOrders"));

    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "true");
    expect(body("WorkOrders")).not.toHaveAttribute("inert");
  });

  it("keeps a closed section mounted but out of reach", async () => {
    const user = userEvent.setup();
    render(<Sidebar runs={[run]} initialSection="chats" />);

    expect(body("WorkOrders")).toHaveAttribute("inert");
    expect(within(body("WorkOrders")).getByRole("link", { name: "+ New WorkOrder" })).toBeVisible();

    await user.click(header("WorkOrders"));

    expect(body("WorkOrders")).not.toHaveAttribute("inert");
    expect(body("Chats")).toHaveAttribute("inert");
  });

  it("puts each section's new button under its own header", () => {
    render(<Sidebar runs={[run]} initialSection="workflows" />);

    expect(within(body("WorkOrders")).getByRole("link", { name: "+ New WorkOrder" })).toHaveAttribute(
      "href",
      "/runs/new",
    );
    expect(within(body("Chats")).getByRole("button", { name: "+ New chat" })).toBeInTheDocument();
    expect(within(body("Projects")).getByRole("link", { name: "+ New project" })).toHaveAttribute(
      "href",
      "/plan",
    );
  });

  it("matches the new workflow button and lists projects by generated name", () => {
    render(
      <Sidebar
        projects={[{ projectId: "project-1", name: "Engine roadmap", archived: false }]}
        runs={[run]}
        initialSection="projects"
      />,
    );

    const newProject = within(body("Projects")).getByRole("link", {
      name: "+ New project",
    });
    expect(newProject).toHaveClass("rail-button", "rail-button-primary");
    expect(within(body("Projects")).getByText("Engine roadmap")).toBeInTheDocument();
  });

  it("marks a project on a milestone child without calling the parent link current", () => {
    const { container } = render(
      <Sidebar
        projects={[
          {
            projectId: "project-1",
            name: "Engine roadmap",
            archived: false,
            conversationUrl: "/conversations/agi-1",
            milestoneCount: 2,
          },
        ]}
        runs={[]}
        initialSection="projects"
        activeProjectId="project-1"
        activeMilestonesPage={false}
      />,
    );

    expect(container.querySelector(".rail-item")).toHaveAttribute("data-active", "true");
    expect(screen.getByRole("link", { name: "Milestones · 2" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  /** A project's only page is the planning conversation it was named after, so
   *  the row is a link to it and the rail marks the one you are reading. */
  it("opens a project's planning conversation and marks the one on screen", () => {
    render(
      <Sidebar
        projects={[
          {
            projectId: "project-agi-1",
            name: "Engine roadmap",
            archived: false,
            conversationUrl: "/conversations/agi-1",
          },
          {
            projectId: "project-agi-2",
            name: "Second roadmap",
            archived: false,
            conversationUrl: "/conversations/agi-2",
          },
        ]}
        runs={[run]}
        initialSection="projects"
        activeConversationUrl="/conversations/agi-1"
      />,
    );

    const open = within(body("Projects")).getByRole("link", { name: "Engine roadmap" });
    expect(open).toHaveAttribute("href", "/conversations/agi-1");
    expect(open).toHaveAttribute("aria-current", "page");
    expect(open.closest(".rail-item")).toHaveAttribute("data-active", "true");
    const other = within(body("Projects")).getByRole("link", { name: "Second roadmap" });
    expect(other).not.toHaveAttribute("aria-current");
    expect(other.closest(".rail-item")).not.toHaveAttribute("data-active");
  });

  /** The rail is drawn beside pages that are nobody's conversation, `/runs`
   *  among them, and there it marks nothing. */
  it("marks no project when no conversation is on screen", () => {
    render(
      <Sidebar
        projects={[
          {
            projectId: "project-agi-1",
            name: "Engine roadmap",
            archived: false,
            conversationUrl: "/conversations/agi-1",
          },
          { projectId: "project-2", name: "Recorded roadmap", archived: false },
        ]}
        runs={[run]}
        initialSection="projects"
      />,
    );

    for (const name of ["Engine roadmap", "Recorded roadmap"])
      expect(
        within(body("Projects")).getByText(name).closest(".rail-item"),
      ).not.toHaveAttribute("data-active");
  });

  /** A project recorded some other way still says it exists, but a row that
   *  leads nowhere should not dress up as something to click. */
  it("lists a project with no conversation as plain text", () => {
    render(
      <Sidebar
        projects={[{ projectId: "project-1", name: "Engine roadmap", archived: false }]}
        runs={[run]}
        initialSection="projects"
      />,
    );

    expect(
      within(body("Projects")).queryByRole("link", { name: "Engine roadmap" }),
    ).not.toBeInTheDocument();
    const row = within(body("Projects")).getByText("Engine roadmap");
    expect(row).toBeInTheDocument();
    // Two absent URLs are not a match: without this the row reads as the page
    // you are on, on every page that is not a conversation.
    expect(row.closest(".rail-item")).not.toHaveAttribute("data-active");
  });

  /** A plan is more than the conversation that wrote it, so a project that has
   *  one offers it: a subheader under the row, opening the milestones page. */
  it("offers the milestones of a project that has some, and marks the open one", () => {
    render(
      <Sidebar
        projects={[
          {
            projectId: "project agi-1",
            name: "Engine roadmap",
            archived: false,
            conversationUrl: "/conversations/agi-1",
            milestoneCount: 3,
          },
          {
            projectId: "project-agi-2",
            name: "Second roadmap",
            archived: false,
            conversationUrl: "/conversations/agi-2",
            milestoneCount: 1,
          },
        ]}
        runs={[run]}
        initialSection="projects"
        activeProjectId="project agi-1"
      />,
    );

    const open = within(body("Projects")).getByRole("link", {
      name: "Milestones · 3",
    });
    // Encoded here rather than spelled by hand: an id with a space in it is
    // not what the store writes, but it is what pins this to one builder.
    expect(open).toHaveAttribute("href", "/projects/project%20agi-1/milestones");
    expect(open).toHaveAttribute("aria-current", "page");
    expect(open.closest(".rail-sub")).toHaveAttribute(
      "aria-label",
      "Milestones for Engine roadmap",
    );
    const other = within(body("Projects")).getByRole("link", { name: "Milestones · 1" });
    expect(other).not.toHaveAttribute("aria-current");
  });

  /** Nothing planned is nothing to open, and a project put away has been put
   *  away along with its plan -- the same reason its row stops being a link. */
  it("offers no milestones for a project without them, or for an archived one", () => {
    render(
      <Sidebar
        projects={[
          { projectId: "project-1", name: "Engine roadmap", archived: false },
          { projectId: "project-2", name: "Fresh plan", archived: false, milestoneCount: 0 },
          { projectId: "project-3", name: "Put away", archived: true, milestoneCount: 4 },
        ]}
        runs={[run]}
        initialSection="projects"
      />,
    );

    expect(
      within(body("Projects")).queryByRole("link", { name: /Milestones/, hidden: true }),
    ).not.toBeInTheDocument();
  });

  /** A plan outlives the conversation that wrote it. Archiving the *thread*
   *  leaves the project itself live, and its milestones are still worth
   *  reading even though its name row has nowhere left to send a click. */
  it("offers the milestones of a live project whose planning chat was archived", () => {
    render(
      <Sidebar
        projects={[
          { projectId: "project-agi-1", name: "Engine roadmap", archived: false, milestoneCount: 2 },
        ]}
        runs={[run]}
        initialSection="projects"
      />,
    );

    const projects = within(body("Projects"));
    expect(projects.queryByRole("link", { name: "Engine roadmap" })).not.toBeInTheDocument();
    expect(projects.getByRole("link", { name: "Milestones · 2" })).toHaveAttribute(
      "href",
      "/projects/project-agi-1/milestones",
    );
  });

  /** Archiving is the same one click a chat gets, and it moves the row into a
   *  list of its own rather than deleting anything. */
  it("archives a project from the rail and lists it under Archived projects", async () => {
    const user = userEvent.setup();
    const archive = vi.fn();
    const active = {
      projectId: "project-agi-1",
      name: "Engine roadmap",
      archived: false,
      conversationUrl: "/conversations/agi-1",
    };
    const { rerender } = render(
      <Sidebar
        projects={[active]}
        runs={[run]}
        initialSection="projects"
        onArchiveProject={archive}
      />,
    );

    expect(within(body("Projects")).queryByText("Archived projects")).not.toBeInTheDocument();
    await user.click(
      within(body("Projects")).getByRole("button", { name: "Archive Engine roadmap" }),
    );
    expect(archive).toHaveBeenCalledWith(active, true);

    rerender(
      <Sidebar
        projects={[{ ...active, archived: true }]}
        runs={[run]}
        initialSection="projects"
        onArchiveProject={archive}
      />,
    );

    const archived = within(body("Projects")).getByText("Engine roadmap");
    expect(archived.closest(".rail-archive")).not.toBeNull();
    expect(within(body("Projects")).getByText("Archived projects")).toBeInTheDocument();
    // Put away is not the page you are reading, so the row stops being a link.
    expect(
      within(body("Projects")).queryByRole("link", {
        name: "Engine roadmap",
        hidden: true,
      }),
    ).not.toBeInTheDocument();

    await user.click(
      within(body("Projects")).getByRole("button", {
        name: "Restore Engine roadmap",
        hidden: true,
      }),
    );
    expect(archive).toHaveBeenLastCalledWith({ ...active, archived: true }, false);
  });

  /** Nothing owns the list on a rail drawn without a handler, so the button is
   *  left out rather than left there doing nothing. */
  it("omits the archive control when no handler is given", () => {
    render(
      <Sidebar
        projects={[{ projectId: "project-1", name: "Engine roadmap", archived: false }]}
        runs={[run]}
        initialSection="projects"
      />,
    );

    expect(
      within(body("Projects")).queryByRole("button", { name: "Archive Engine roadmap" }),
    ).not.toBeInTheDocument();
  });

  it("lists runs with their conversations and marks the one on screen", () => {
    render(<Sidebar runs={[run]} initialSection="workflows" activeRunId="run-1" />);

    const entry = within(body("WorkOrders")).getByRole("link", { name: /First run/ });
    expect(entry).toHaveAttribute("href", "/runs/run-1");
    expect(entry).toHaveTextContent("Implementation · v1");
    expect(entry.closest(".rail-item")).toHaveAttribute("data-active", "true");
    expect(
      within(body("WorkOrders")).getByRole("link", { name: "Implementation conversation" }),
    ).toHaveAttribute("href", "/runs/run-1/conversations/conversation");
  });

  it("marks the open conversation rather than the run it belongs to", () => {
    render(
      <Sidebar
        runs={[run]}
        initialSection="workflows"
        activeRunId="run-1"
        activeConversationUrl="/runs/run-1/conversations/conversation"
      />,
    );

    const entry = within(body("WorkOrders")).getByRole("link", { name: /First run/ });
    expect(entry.closest(".rail-item")).not.toHaveAttribute("data-active");
    expect(
      within(body("WorkOrders")).getByRole("link", { name: "Implementation conversation" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("marks a workflow conversation that is waiting for input", () => {
    const waiting = {
      ...run,
      steps: [{ ...run.steps[0], waiting: true }],
    };
    render(<Sidebar runs={[waiting]} initialSection="workflows" />);

    const entry = within(body("WorkOrders")).getByRole("link", {
      name: "Implementation conversation Waiting for input",
    });
    expect(entry).toHaveTextContent("Implementation conversation ❔");
  });

  /** Switching the open conversation in place only means something beside a
   *  chat of its own; from anywhere else a chat is a link to its own page. */
  it("links chats away from a page that is not a standalone chat", () => {
    const { rerender } = render(<Sidebar runs={[]} initialSection="chats" linkChats />);

    expect(within(body("Chats")).getByRole("link", { name: "+ New chat" })).toHaveAttribute(
      "href",
      "/conversations",
    );
    expect(within(body("Chats")).getByRole("link", { name: /First chat/ })).toHaveAttribute(
      "href",
      "/conversations/thread-1",
    );

    rerender(<Sidebar runs={[]} initialSection="chats" />);

    expect(within(body("Chats")).getByRole("button", { name: "+ New chat" })).toBeInTheDocument();
    expect(within(body("Chats")).getByRole("button", { name: /First chat/ })).toBeInTheDocument();
  });
});
