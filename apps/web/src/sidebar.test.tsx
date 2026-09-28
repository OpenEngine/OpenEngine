import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiGraphTopology, ApiWorkflowRun } from "./api";
import { Sidebar } from "./sidebar";

const run: ApiWorkflowRun = {
  runId: "run-1",
  name: "First run",
  workflowId: "implementation-review-codex",
  workflowName: "Implementation review (codex)",
  taskId: "task-1",
  taskPrompt: "Do the work",
  repository: ".",
  repositoryContext: { repository: "." },
  phase: "running_agent",
  terminalOutcome: null,
  failureReason: "",
  graphProgress: {
    activeNodeIds: ["implementation"],
    waitingNodeIds: [],
    nextNodeIds: [],
  },
};

/** A second WorkOrder of the same workflow, for the tests about two rows. */
const graphRun: ApiWorkflowRun = {
  ...run,
  runId: "run-2",
  name: "Second run",
};

/** Its graph, as the engine describes one: the two agents a person can read,
 *  and the two stages that are not conversations. */
const nodes: ApiGraphTopology["nodes"] = [
  { nodeId: "workspace", name: "Workspace", kind: "workspace", showInSidebar: false },
  { nodeId: "implementation", name: "Implementation", kind: "agent" },
  { nodeId: "review", name: "Review", kind: "agent", showInSidebar: true },
  { nodeId: "human-review", name: "Human review", kind: "human", showInSidebar: false },
];

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

  it("filters multiple stages and outcomes without toggling the accordion", async () => {
    const user = userEvent.setup();
    const at = (id: string) =>
      ({ activeNodeIds: [id], waitingNodeIds: [], nextNodeIds: [] });
    const runs = [run,
      { ...run, runId: "review", name: "Review run", graphProgress: at("review") },
      { ...run, runId: "human", name: "Human run", phase: "awaiting_human_review",
        graphProgress: at("human-review") },
      { ...run, runId: "failed", name: "Failed run", phase: "failed" },
      { ...run, runId: "succeeded", name: "Succeeded run", phase: "succeeded" },
    ];
    const graphNodes = { [run.workflowId]: nodes };
    const { rerender } = render(
      <Sidebar runs={runs} graphNodes={graphNodes} initialSection="workflows" />,
    );
    await user.click(header("Filter WorkOrders"));
    expect(header("WorkOrders")).toHaveAttribute("aria-expanded", "true");
    // The accepted run is done, so it is in the archive rather than the rail
    // until the filter asks for it.
    expect(screen.queryByRole("link", { name: /Succeeded run/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "succeeded" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Archived" }));
    expect(screen.getByRole("link", { name: /Succeeded run/ })).toBeVisible();
    for (const name of ["Implementation", "Review", "Human review", "failed", "succeeded"]) {
      expect(screen.getByRole("checkbox", { name })).toBeChecked();
    }
    await user.click(screen.getByRole("checkbox", { name: "succeeded" }));
    expect(screen.queryByRole("link", { name: /Succeeded run/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Failed run/ })).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "Review" }));
    expect(screen.queryByRole("link", { name: /Review run/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Human run/ })).toBeVisible();
    rerender(<Sidebar runs={[...runs]} graphNodes={graphNodes} initialSection="workflows" />);
    expect(screen.queryByRole("link", { name: /Succeeded run/ })).not.toBeInTheDocument();
    for (const name of ["Implementation", "Human review", "failed"]) {
      await user.click(screen.getByRole("checkbox", { name }));
    }
    expect(screen.getByText("No WorkOrders match the selected filters.")).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "succeeded" }));
    expect(screen.getByRole("link", { name: /Succeeded run/ })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "WorkOrder filters" })).not.toBeInTheDocument();
    expect(header("Filter WorkOrders")).toHaveFocus();
  });

  it("derives unique options from history and preserves exclusions across refreshes", async () => {
    const user = userEvent.setup();
    const graphNodes = { [run.workflowId]: nodes };
    const custom = run;
    const { rerender } = render(
      <Sidebar runs={[]} graphNodes={graphNodes} initialSection="workflows" />,
    );
    await user.click(header("Filter WorkOrders"));
    expect(screen.getByText("No WorkOrder history yet.")).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "Archived" }));
    rerender(<Sidebar runs={[custom, { ...custom, runId: "duplicate" }]}
      graphNodes={graphNodes} initialSection="workflows" />);
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
    await user.click(screen.getByRole("checkbox", { name: "Implementation" }));
    expect(screen.queryByRole("link", { name: /First run/ })).not.toBeInTheDocument();
    const finished = { ...run, runId: "done", name: "Finished run", phase: "succeeded" };
    rerender(<Sidebar runs={[finished]} graphNodes={graphNodes} initialSection="workflows" />);
    expect(screen.queryByRole("checkbox", { name: "Implementation" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "succeeded" })).toBeChecked();
    rerender(<Sidebar runs={[custom, finished]} graphNodes={graphNodes} initialSection="workflows" />);
    expect(screen.getByRole("checkbox", { name: "Implementation" })).not.toBeChecked();
    expect(screen.getByRole("link", { name: /Finished run/ })).toBeVisible();
  });

  it("filters graph frontiers and gives terminal outcomes priority", async () => {
    const user = userEvent.setup();
    const graphProgress = { activeNodeIds: ["review"], waitingNodeIds: [], nextNodeIds: [] };
    const { rerender } = render(<Sidebar runs={[{ ...graphRun, graphProgress }]}
      graphNodes={{ [graphRun.workflowId]: nodes }} initialSection="workflows" />);
    await user.click(header("Filter WorkOrders"));
    await user.click(screen.getByRole("checkbox", { name: "Archived" }));
    await user.click(screen.getByRole("checkbox", { name: "Review" }));
    expect(screen.queryByRole("link", { name: /Second run/ })).not.toBeInTheDocument();
    rerender(<Sidebar runs={[{ ...graphRun, phase: "succeeded", graphProgress }]}
      graphNodes={{ [graphRun.workflowId]: nodes }} initialSection="workflows" />);
    expect(screen.getByRole("link", { name: /Second run/ })).toBeVisible();
    await user.click(screen.getByRole("checkbox", { name: "succeeded" }));
    expect(screen.queryByRole("link", { name: /Second run/ })).not.toBeInTheDocument();
  });

  it("lists runs with their conversations and marks the one on screen", () => {
    render(<Sidebar runs={[run]} graphNodes={{ [run.workflowId]: nodes }} initialSection="workflows" activeRunId="run-1" />);

    const entry = within(body("WorkOrders")).getByRole("link", { name: /First run/ });
    expect(entry).toHaveAttribute("href", "/runs/run-1");
    expect(entry).toHaveTextContent("Implementation · implementation-review-codex");
    expect(entry.closest(".rail-item")).toHaveAttribute("data-active", "true");
    expect(
      within(body("WorkOrders")).getByRole("link", { name: "Implementation" }),
    ).toHaveAttribute("href", "/runs/run-1/conversations/graph--implementation");
  });

  it("marks the open conversation rather than the run it belongs to", () => {
    render(
      <Sidebar
        runs={[run]} graphNodes={{ [run.workflowId]: nodes }}
        initialSection="workflows"
        activeRunId="run-1"
        activeConversationUrl="/runs/run-1/conversations/graph--implementation"
      />,
    );

    const entry = within(body("WorkOrders")).getByRole("link", { name: /First run/ });
    expect(entry.closest(".rail-item")).not.toHaveAttribute("data-active");
    expect(
      within(body("WorkOrders")).getByRole("link", { name: "Implementation" }),
    ).toHaveAttribute("aria-current", "page");
  });

  /** The project row's ×, on a WorkOrder. What it hands back is the whole run,
   *  because throwing one away is the owner's call to make, not the rail's. */
  it("deletes a WorkOrder from the rail", async () => {
    const user = userEvent.setup();
    const remove = vi.fn();
    render(
      <Sidebar runs={[run]} initialSection="workflows" onDeleteRun={remove} />,
    );

    await user.click(
      within(body("WorkOrders")).getByRole("button", { name: "Delete First run" }),
    );

    expect(remove).toHaveBeenCalledWith(run);
  });

  it("omits the delete control when no handler is given", () => {
    render(<Sidebar runs={[run]} initialSection="workflows" />);

    expect(
      within(body("WorkOrders")).queryByRole("button", { name: "Delete First run" }),
    ).not.toBeInTheDocument();
  });

  /** A graph WorkOrder has no steps -- a graph is not made of them -- so the
   *  shortcuts under its name are its graph's nodes, offered from the moment
   *  the run exists rather than once an agent has said something. */
  it("offers a graph WorkOrder's nodes as its conversations", () => {
    render(
      <Sidebar
        runs={[graphRun]}
        graphNodes={{ "implementation-review-codex": nodes }}
        initialSection="workflows"
        activeRunId="run-2"
      />,
    );

    const rail = within(body("WorkOrders"));
    expect(rail.getByRole("link", { name: "Implementation" })).toHaveAttribute(
      "href",
      "/runs/run-2/conversations/graph--implementation",
    );
    expect(rail.getByRole("link", { name: "Review" })).toHaveAttribute(
      "href",
      "/runs/run-2/conversations/graph--review",
    );
  });

  it("collapses related graph conversations under their node group", async () => {
    const user = userEvent.setup();
    const reviewNodes: ApiGraphTopology["nodes"] = [
      nodes[1],
      {
        nodeId: "review-security",
        name: "Review (Security)",
        kind: "agent",
        group: "Review",
      },
      {
        nodeId: "review-performance",
        name: "Review (Performance)",
        kind: "agent",
        group: "Review",
      },
    ];
    render(
      <Sidebar
        runs={[graphRun]}
        graphNodes={{ [graphRun.workflowId]: reviewNodes }}
        initialSection="workflows"
      />,
    );

    const rail = within(body("WorkOrders"));
    const group = rail.getByText("Review", { selector: "summary" });
    expect(rail.getByRole("link", { name: "Review (Security)" })).not.toBeVisible();

    await user.click(group);

    expect(rail.getByRole("link", { name: "Review (Security)" })).toHaveAttribute(
      "href",
      "/runs/run-2/conversations/graph--review-security",
    );
    expect(rail.getByRole("link", { name: "Review (Performance)" })).toHaveAttribute(
      "href",
      "/runs/run-2/conversations/graph--review-performance",
    );
  });

  /** The checkout and the person's own verdict are stages, not conversations,
   *  and each says so about itself. */
  it("leaves out the nodes that say they do not belong in the rail", () => {
    render(
      <Sidebar
        runs={[graphRun]}
        graphNodes={{ "implementation-review-codex": nodes }}
        initialSection="workflows"
      />,
    );

    const rail = within(body("WorkOrders"));
    expect(rail.queryByRole("link", { name: "Workspace" })).not.toBeInTheDocument();
    expect(rail.queryByRole("link", { name: "Human review" })).not.toBeInTheDocument();
  });

  it("marks the graph conversation on screen rather than the run it belongs to", () => {
    render(
      <Sidebar
        runs={[graphRun]}
        graphNodes={{ "implementation-review-codex": nodes }}
        initialSection="workflows"
        activeRunId="run-2"
        activeConversationUrl="/runs/run-2/conversations/graph--review"
      />,
    );

    const rail = within(body("WorkOrders"));
    expect(
      rail.getByRole("link", { name: /Second run/ }).closest(".rail-item"),
    ).not.toHaveAttribute("data-active");
    expect(rail.getByRole("link", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(rail.getByRole("link", { name: "Implementation" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  /** The graph engine may not be running, or may not have answered yet. The
   *  rail is then the one it was before these were offered at all. */
  it("offers nothing under a graph WorkOrder whose nodes are unknown", () => {
    render(<Sidebar runs={[graphRun]} initialSection="workflows" />);

    expect(
      within(body("WorkOrders")).queryByLabelText("Conversations for Second run"),
    ).not.toBeInTheDocument();
  });

  it.each([
    { activeNodeIds: ["implementation"], waitingNodeIds: [], nextNodeIds: ["review"], label: "Implementation", live: true },
    { activeNodeIds: ["implementation"], waitingNodeIds: ["implementation"], nextNodeIds: ["review"], label: "Implementation", live: true },
    { activeNodeIds: [], waitingNodeIds: ["human-review"], nextNodeIds: ["human-review"], label: "Human review", live: false },
    { activeNodeIds: [], waitingNodeIds: [], nextNodeIds: ["human-review"], label: "Human review", live: false },
    { activeNodeIds: ["implementation", "review", "review"], waitingNodeIds: ["review"], nextNodeIds: ["human-review"], label: "Implementation, Review", live: true },
  ])("shows graph status $label with activity=$live", ({ label, live, ...graphProgress }) => {
    render(
      <Sidebar
        runs={[{ ...graphRun, graphProgress }]}
        graphNodes={{ [graphRun.workflowId]: nodes }}
        initialSection="workflows"
      />,
    );

    const entry = screen.getByRole("link", { name: /Second run/ });
    expect(entry).toHaveTextContent(`${label} · ${graphRun.workflowId}`);
    expect(within(entry).queryByLabelText("WorkOrder is in progress") !== null).toBe(live);
    for (const name of ["Implementation", "Review"]) {
      // jsdom versions differ on spacing before the inline marker's accessible name.
      const conversation = screen.getByRole("link", { name: new RegExp(`^${name}(\\s*Waiting for input)?$`) });
      expect(within(conversation).queryByLabelText("Waiting for input") !== null)
        .toBe(graphProgress.waitingNodeIds.includes(name.toLowerCase()));
    }
  });

  it("clears graph activity and approval markers when the run finishes", async () => {
    const user = userEvent.setup();
    render(
      <Sidebar
        runs={[{ ...graphRun, phase: "succeeded", graphProgress: {
          activeNodeIds: ["implementation"], waitingNodeIds: ["implementation"], nextNodeIds: [],
        } }]}
        graphNodes={{ [graphRun.workflowId]: nodes }}
        initialSection="workflows"
      />,
    );
    // An accepted run is archived, so the row this is about is read there.
    await user.click(header("Filter WorkOrders"));
    await user.click(screen.getByRole("checkbox", { name: "Archived" }));

    expect(screen.getByRole("link", { name: /Second run/ })).toHaveTextContent("succeeded ·");
    expect(screen.queryByLabelText("WorkOrder is in progress")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Waiting for input")).not.toBeInTheDocument();
  });

  /** The foot's second control, beside the gear rather than inside it: what a
   *  runner has spent is a page, not a setting. */
  it("offers the utilization page from the rail foot and marks it when open", () => {
    const { rerender } = render(<Sidebar runs={[run]} initialSection="workflows" />);

    const link = screen.getByRole("link", { name: "Open utilization" });
    expect(link).toHaveAttribute("href", "/utilization");
    expect(link).not.toHaveAttribute("aria-current");

    rerender(
      <Sidebar runs={[run]} initialSection="workflows" activeView="utilization" />,
    );

    expect(screen.getByRole("link", { name: "Open utilization" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});


it("hides scheduled workorders and shows them after starting", () => {
  const { rerender } = render(<Sidebar initialSection="workflows" runs={[{ ...run, phase: "scheduled" }]} />);
  expect(screen.queryByText("First run")).not.toBeInTheDocument();
  rerender(<Sidebar initialSection="workflows" runs={[run]} />);
  expect(screen.getByText("First run")).toBeInTheDocument();
});

it("shows WorkOrders without a Projects accordion or creation link", () => {
  render(<Sidebar runs={[run]} />);
  expect(screen.getByRole("button", { name: "WorkOrders" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("link", { name: "+ New WorkOrder" })).toHaveAttribute("href", "/runs/new");
  expect(screen.queryByRole("button", { name: "Projects" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /New project/i })).not.toBeInTheDocument();
});
