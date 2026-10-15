import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentList } from "./agents";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function agent(
  name: string,
  health: { responding: boolean; modelLoaded: boolean | null } | null,
  url = `http://${name}.local:11434/v1`,
) {
  return {
    name,
    kind: "opencode",
    model: "qwen3-coder",
    url,
    builtin: false,
    createdAt: "2026-10-14T12:00:00Z",
    updatedAt: "2026-10-14T12:00:00Z",
    health,
  };
}

/** The graph service's agents, mocked: no test reaches a model endpoint. */
function server(agents: unknown[]) {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/v1/agents") return json({ agents });
    throw new Error(`unexpected request to ${path}`);
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

async function row(name: string) {
  return screen.findByLabelText(`${name} agent`);
}

describe("AgentList", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks the agents API for the agents and their endpoint health", async () => {
    const fetcher = server([agent("qwen", { responding: true, modelLoaded: true })]);
    render(<AgentList />);

    const qwen = await row("qwen");
    expect(within(qwen).getByText("qwen3-coder")).toBeInTheDocument();
    expect(fetcher.mock.calls.map(([input]) => String(input))).toContain("/api/v1/agents");
  });

  it("shows a responding endpoint with the model loaded", async () => {
    server([agent("qwen", { responding: true, modelLoaded: true })]);
    render(<AgentList />);

    const qwen = await row("qwen");
    expect(within(qwen).getByText("Responding")).toBeInTheDocument();
    expect(within(qwen).getByText("Model loaded")).toBeInTheDocument();
    expect(within(qwen).queryByText("Not responding")).not.toBeInTheDocument();
  });

  it("shows a responding endpoint whose model is not loaded yet", async () => {
    server([agent("qwen", { responding: true, modelLoaded: false })]);
    render(<AgentList />);

    const qwen = await row("qwen");
    expect(within(qwen).getByText("Responding")).toBeInTheDocument();
    expect(within(qwen).getByText("Model not loaded")).toBeInTheDocument();
  });

  it("says when the endpoint cannot tell whether the model is loaded", async () => {
    server([agent("vllm", { responding: true, modelLoaded: null })]);
    render(<AgentList />);

    const vllm = await row("vllm");
    expect(within(vllm).getByText("Responding")).toBeInTheDocument();
    expect(within(vllm).getByText("Model status unknown")).toBeInTheDocument();
  });

  it("shows an endpoint that does not respond", async () => {
    server([agent("dead", { responding: false, modelLoaded: null })]);
    render(<AgentList />);

    const dead = await row("dead");
    expect(within(dead).getByText("Not responding")).toBeInTheDocument();
    expect(within(dead).queryByText("Model loaded")).not.toBeInTheDocument();
  });

  it("shows each url agent's own state", async () => {
    server([
      agent("dead", { responding: false, modelLoaded: null }),
      agent("qwen", { responding: true, modelLoaded: true }),
    ]);
    render(<AgentList />);

    expect(within(await row("dead")).getByText("Not responding")).toBeInTheDocument();
    expect(within(await row("qwen")).getByText("Responding")).toBeInTheDocument();
    expect(within(await row("qwen")).getByText("Model loaded")).toBeInTheDocument();
  });

  it("shows no endpoint health for an agent without a url", async () => {
    server([
      { name: "claude", kind: "claude", model: "", url: "", builtin: true, health: null },
      agent("hosted", null, ""),
    ]);
    render(<AgentList />);

    for (const name of ["claude", "hosted"]) {
      const entry = await row(name);
      expect(within(entry).queryByText(/responding/i)).not.toBeInTheDocument();
      expect(within(entry).queryByText(/^model /i)).not.toBeInTheDocument();
    }
  });
});
