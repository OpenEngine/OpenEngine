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
  fireEvent.change(screen.getByLabelText("Repository URL or local path"), { target: { value: "https://github.com/owner/repo" } });
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "disconnected" } });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Cloning");
  await waitFor(() => expect(screen.getByLabelText("Repository")).toHaveValue("/code/repo"), { timeout: 3000 });
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ repository: "https://github.com/owner/repo", mode: "disconnected", trusted: false });
  expect(fetch.mock.calls[1][0]).toBe("/api/repositories/onboard/job1");
  expect(screen.getByRole("button", { name: "Add repository" })).toBeInTheDocument();
});

it("shows a preflight fix and permits a retry", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "failed", message: "Preflight failed", problems: [{ code: "agent", message: "No agent signed in", fix: "oe agent signin codex" }],
  }) }));
  render(<AddRepository onComplete={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Repository URL or local path"), { target: { value: "owner/repo" } });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("oe agent signin codex");
  expect(screen.getByRole("button", { name: "Add repository" })).toBeEnabled();
});


it("persists an explicit trust choice", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "failed", message: "Retry", problems: [],
  }) });
  vi.stubGlobal("fetch", fetch);
  render(<AddRepository onComplete={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Repository URL or local path"), { target: { value: "owner/repo" } });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  await screen.findByRole("status");
  expect(JSON.parse(fetch.mock.calls[0][1].body).trusted).toBe(true);
});

it("submits a local path with automatic mode", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "failed", message: "Retry", problems: [],
  }) });
  vi.stubGlobal("fetch", fetch);
  render(<AddRepository onComplete={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Repository URL or local path"), { target: { value: "/code/demo" } });
  fireEvent.click(screen.getByRole("button", { name: "Add repository" }));
  await screen.findByRole("status");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ repository: "/code/demo", trusted: false });
});
