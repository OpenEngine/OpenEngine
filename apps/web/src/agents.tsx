/** The agents this engine runs work through, and how each one's own model
 *  endpoint is doing right now.
 *
 *  An opencode agent can point at a local model server -- an Ollama or an
 *  OpenAI-compatible one -- and that server is a thing that can be down or be
 *  up without the model anyone asked for in hand. The poller in the graph
 *  service answers both questions every so often and hands them back on each
 *  agent as `health`, so the rail can say which agents can actually run a turn
 *  today rather than only which are configured.
 *
 *  Read beside the rail's list of agents: it is the only place that knows which
 *  of them reach a live endpoint, so without it a configured agent looks as ready
 *  as one whose server has long gone quiet.
 */

import { useEffect, useState } from "react";

/** What the poller reports for one endpoint, in the engine's own words: is the
 *  server answering, and does it have this agent's model loaded. `modelLoaded`
 *  is null when the server answers but cannot say -- it lists no model, or it
 *  withholds the list behind a sign-in this agent is not signed in to. */
export type AgentHealth = { responding: boolean; modelLoaded: boolean | null };

export type Agent = {
  name: string;
  kind: string;
  model: string;
  url: string;
  builtin: boolean;
  health: AgentHealth | null;
};

/** How long between asks, in milliseconds. The endpoint changes slowly, so a
 *  slow poll keeps the rail honest without hammering the model server. */
const POLL_MS = 15000;

/** The two lines one row draws for its endpoint, or nothing for an agent with
 *  no endpoint -- a built-in, or one with no url -- which reports no health. */
function healthText(health: AgentHealth | null): { status?: string; model?: string } {
  if (!health) return {};
  if (!health.responding) return { status: "Not responding" };
  if (health.modelLoaded === true) return { status: "Responding", model: "Model loaded" };
  if (health.modelLoaded === false) return { status: "Responding", model: "Model not loaded" };
  return { status: "Responding", model: "Model status unknown" };
}

function AgentEndpointRow({ agent }: { agent: Agent }) {
  const health = healthText(agent.health);
  return (
    <li className="agent" aria-label={`${agent.name} agent`}>
      <div className="agent-head">
        <span className="agent-name">{agent.name}</span>
        {agent.model && <span className="agent-model">{agent.model}</span>}
      </div>
      {/* Without an endpoint there is no health to read, so neither line shows. */}
      {health.status && <span className="agent-health">{health.status}</span>}
      {health.model && <span className="agent-model-state">{health.model}</span>}
    </li>
  );
}

/** The rail's list of every agent this engine can run work through, each with
 *  its endpoint's current health.
 *
 *  A later poll that fails keeps what is on screen, because the agents are still
 *  true -- only the reading of which server answered this turn is gone. */
export function AgentList() {
  const [agents, setAgents] = useState<Agent[]>([]);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      fetch("/api/v1/agents")
        .then((response) => response.json())
        .then((body) => {
          if (cancelled) return;
          setAgents(Array.isArray(body.agents) ? body.agents : []);
        })
        .catch(() => {})
        .finally(() => {
          if (!cancelled) timer = window.setTimeout(load, POLL_MS);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  return (
    <ul className="agents">
      {agents.map((agent) => (
        <AgentEndpointRow agent={agent} key={agent.name} />
      ))}
    </ul>
  );
}
