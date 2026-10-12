import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AddRepository } from "./onboarding";
import { NewWorkflowPage } from "./runs";
import type { EngineConfig } from "./api";

const config: EngineConfig = {
  agents: [], runners: [], defaultAgent: "a", defaultRunner: "r", hasRepositories: true,
  repositories: [{ name: "owner/repo", path: "/code/repo" }],
  workflows: [{ id: "work", name: "Work" }],
};
afterEach(() => vi.unstubAllGlobals());

it("submits the URL and mode, polls, then selects the new checkout in the WorkOrder form", async () => {
  const job = { id: "job1", repository: "owner/repo", path: "/code/repo", status: "cloning", message: "Cloning", problems: [] };
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: true, json: async () => job })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ ...job, status: "complete", message: "Repository onboarded" }) })
    .mockResolvedValueOnce({ ok: true, json: async () => config });
  vi.stubGlobal("fetch", fetch);
  render(<NewWorkflowPage config={{ ...config, hasRepositories: false, repositories: [] }} />);
  expect(screen.queryByText("Create a WorkOrder")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("GitHub URL"), { target: { value: "https://github.com/owner/repo" } });
  fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "disconnected" } });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Cloning");
  await waitFor(() => expect(screen.getByLabelText("Repository")).toHaveValue("/code/repo"), { timeout: 3000 });
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ repository: "https://github.com/owner/repo", mode: "disconnected" });
  expect(fetch.mock.calls[1][0]).toBe("/api/repositories/onboard/job1");
  expect(screen.getByRole("button", { name: "Add repository" })).toBeInTheDocument();
});

it("shows a preflight fix and permits a retry", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "failed", message: "Preflight failed", problems: [{ code: "agent", message: "No agent signed in", fix: "engine agent signin codex" }],
  }) }));
  render(<AddRepository onComplete={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("GitHub URL"), { target: { value: "owner/repo" } });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("engine agent signin codex");
  expect(screen.getByRole("button", { name: "Add repository" })).toBeEnabled();
});
