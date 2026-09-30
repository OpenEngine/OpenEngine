import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Sidebar } from "./sidebar";

const config = { repositories: [{ name: "Engine", path: "/engine" }], workflows: [{ id: "review", name: "Review" }] };
afterEach(() => vi.unstubAllGlobals());

it("creates a project from the sidebar and edits its scheduling settings", async () => {
  let saved: Record<string, unknown> | undefined;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/config") return Response.json(config);
    if (init?.method === "POST" || init?.method === "PATCH") {
      saved = { ...JSON.parse(String(init.body)), project_id: "p1", used_budget: 0, last_run_id: "run-1", error: "" };
      return Response.json(saved);
    }
    return Response.json(saved ? [saved] : []);
  });
  vi.stubGlobal("fetch", fetcher);
  const user = userEvent.setup();
  render(<Sidebar runs={[]} />);
  await user.click(screen.getByRole("button", { name: "Projects" }));
  await user.click(await screen.findByRole("button", { name: "+ New project" }));
  await user.type(screen.getByLabelText("Name"), "Maintain tests");
  await user.type(screen.getByLabelText("Agent instructions"), "Find and cover missing tests.");
  await user.clear(screen.getByLabelText("Timezone"));
  await user.type(screen.getByLabelText("Timezone"), "America/Denver");
  await user.clear(screen.getByLabelText("Daily budget (WorkOrders)"));
  await user.type(screen.getByLabelText("Daily budget (WorkOrders)"), "3");
  await user.click(screen.getByLabelText("Enable autonomous scheduling"));
  await user.click(screen.getByRole("button", { name: "Save project" }));
  await screen.findByText("Scheduled · 0/3 WorkOrders today");
  expect(saved).toMatchObject({ name: "Maintain tests", instructions: "Find and cover missing tests.",
    daily_budget: 3, timezone: "America/Denver", weekdays: [0, 1, 2, 3, 4], enabled: true, repository: "/engine", workflow: "review" });
  expect(screen.getByRole("link", { name: "Latest WorkOrder" })).toHaveAttribute("href", "/runs/run-1");
  await user.click(screen.getByRole("button", { name: /Maintain tests/ }));
  await user.click(screen.getByLabelText("Enable autonomous scheduling"));
  await user.click(screen.getByRole("button", { name: "Save project" }));
  await screen.findByText("Paused · 0/3 WorkOrders today");
  expect(saved?.enabled).toBe(false);
});

it("keeps the draft when saving fails", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/config") return Response.json(config);
    if (init?.method === "POST") return Response.json({ error: "select at least one weekday" }, { status: 400 });
    return Response.json([]);
  }));
  const user = userEvent.setup();
  render(<Sidebar runs={[]} initialSection="projects" />);
  await waitFor(() => expect(screen.getByRole("button", { name: "+ New project" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "+ New project" }));
  await user.type(screen.getByLabelText("Name"), "Tests");
  await user.type(screen.getByLabelText("Agent instructions"), "Add tests");
  await user.click(screen.getByRole("button", { name: "Save project" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("select at least one weekday");
  expect(screen.getByLabelText("Agent instructions")).toHaveValue("Add tests");
});
