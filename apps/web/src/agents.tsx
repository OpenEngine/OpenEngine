/** The agents this engine has registered, and the state of each one's model endpoint.
 *
 *  A URL agent points at a model server the engine does not own, so nothing
 *  here knows whether that server is up or whether it is actually running the
 *  model it was told to run: the graph service probes the endpoints and reports
 *  the answer, and this panel only reads it back. A builtin or hosted agent has
 *  no endpoint of its own, so it carries no health at all.
 */

import { useEffect, useState } from "react";

import { api } from "./api";

/** What the poller last found out about one endpoint, or `null` for an agent
 *  that has no endpoint to probe. */
type AgentHealth = {
  responding: boolean;
  modelLoaded: boolean | null;
};

type AgentRow = {
  name: string;
  kind: string;
  model: string;
  url: string;
  builtin: boolean;
  createdAt: string;
  updatedAt: string;
  health: AgentHealth | null;
};

/** What the endpoint says about the model, in the reader's words rather than
 *  the wire's. `null` is a server that answers but will not say which of its
 *  models are loaded, which is a different thing from one that does not answer. */
function modelStatus(health: AgentHealth): string {
  if (health.modelLoaded === true) return "Model loaded";
  if (health.modelLoaded === false) return "Model not loaded";
  return "Model status unknown";
}

function AgentEntry({ agent }: { agent: AgentRow }) {
  const { health } = agent;
  return (
    <li className="agent-entry" aria-label={`${agent.name} agent`}>
      <div className="agent-entry-head">
        <strong>{agent.name}</strong>
        {agent.model && <span className="agent-model">{agent.model}</span>}
      </div>
      {/* Only an agent with an endpoint has anything to say about it; a builtin
          or hosted row would be reading a health out of an empty field. */}
      {health !== null && (
        <div className="agent-health">
          <span className={`chip ${health.responding ? "" : "chip-flame"}`}>
            {health.responding ? "Responding" : "Not responding"}
          </span>
          <span className="agent-model-state" data-tone={health.modelLoaded === true ? "live" : undefined}>
            {modelStatus(health)}
          </span>
        </div>
      )}
    </li>
  );
}

/** The registered agents and the state of each URL agent's model endpoint. */
export function AgentList() {
  const [agents, setAgents] = useState<AgentRow[] | undefined>();
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api<{ agents: AgentRow[] }>("/api/v1/agents")
      .then((body) => {
        if (cancelled) return;
        setAgents(body.agents);
        setError("");
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <p className="notice">Could not read the agents: {error}</p>;
  if (!agents) return null;
  if (agents.length === 0) return <p className="state-inline">No agents are registered.</p>;

  return (
    <ul className="agent-list">
      {agents.map((agent) => (
        <AgentEntry agent={agent} key={agent.name} />
      ))}
    </ul>
  );
}
