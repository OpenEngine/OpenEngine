/** The added agents, and whether each one's model endpoint is up and loaded.
 *
 *  An agent added with `engine agent add --url` runs on an OpenAI-compatible
 *  model server -- Ollama, a local vLLM, a remote endpoint. Its reachability
 *  and whether the model is loaded in memory are not part of the agent row, so
 *  the engine probes every url endpoint on a cadence and serves the reading on
 *  the agent as `health`. This list says what that reading found: "the endpoint
 *  answers and the model is loaded", the cold-load case "the endpoint answers
 *  but the model is not loaded yet", or "the endpoint is not responding". An
 *  agent without a url is not on its own endpoint and has no reading. */

import { useEffect, useState } from "react";

type EndpointHealth = {
  responding: boolean;
  modelLoaded: boolean | null;
};

/** One agent the engine offers, as `GET /api/v1/agents` serves it.
 *
 *  `health` is the most recent endpoint reading of an agent with a url -- its
 *  reachability and whether its model is loaded -- and `null` for an agent with
 *  no own endpoint. */
export type ApiAgent = {
  name: string;
  kind: string;
  model: string;
  url: string;
  builtin: boolean;
  createdAt: string;
  updatedAt: string;
  health: EndpointHealth | null;
};

const AGENTS_PATH = "/api/v1/agents";

/** Read the agents the engine offers. The list fetches its own copy: the rail
 *  that draws it would otherwise have to thread the probe's cadence through a
 *  prop, and the engine's list is the one source of the readings. */
async function listAgents(): Promise<ApiAgent[]> {
  const response = await fetch(AGENTS_PATH, {
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return (await response.json()).agents;
}

/** What a probe found, read on its own: the endpoint and the model, each on its
 *  own line of the badge. */
function Health({ health }: { health: EndpointHealth }) {
  const modelText =
    health.modelLoaded === true
      ? "Model loaded"
      : health.modelLoaded === false
        ? "Model not loaded"
        : "Model status unknown";
  return (
    <div className="agent-health" aria-label="endpoint health">
      <span data-responding={health.responding ? "yes" : "no"}>
        {health.responding ? "Responding" : "Not responding"}
      </span>
      <span data-model-loaded={String(health.modelLoaded)}>{modelText}</span>
    </div>
  );
}

function Agent({ agent }: { agent: ApiAgent }) {
  return (
    <li aria-label={`${agent.name} agent`}>
      <span className="agent-name">{agent.name}</span>
      <span className="agent-kind">{agent.kind}</span>
      <span className="agent-model">{agent.model}</span>
      {agent.url && agent.health && <Health health={agent.health} />}
    </li>
  );
}

/** The added agents, each with its endpoint's reachability and loaded model.
 *
 *  The engine probes every url endpoint on a cadence and serves the reading on
 *  the agent, so this list only has to read the agents -- its own copy, since
 *  the engine's is the one source of the readings. */
export function AgentList() {
  const [agents, setAgents] = useState<ApiAgent[]>([]);

  useEffect(() => {
    let current = true;
    void listAgents()
      .then((listed) => {
        if (current) setAgents(listed);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, []);

  return (
    <ul aria-label="Agents" className="agents">
      {agents.map((agent) => (
        <Agent key={agent.name} agent={agent} />
      ))}
    </ul>
  );
}
