/** The agents this engine has registered, and the state of each one's model endpoint.
 *
 *  A URL agent points at a model server the engine does not own, so nothing
 *  here knows whether that server is up or whether it is actually running the
 *  model it was told to run: the graph service probes the endpoints and reports
 *  the answer, and this panel only reads it back. A builtin or hosted agent has
 *  no endpoint of its own, so it carries no health at all.
 */

import { useEffect, useState } from "react";

import { getAgents, type AgentHealth, type AgentRow } from "./api";

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
          or hosted row would be reading a health out of an empty field. A URL
          agent the poller has not reached yet is still being checked. */}
      {health === null && agent.url && (
        <div className="agent-health">
          <span className="chip">Checking endpoint</span>
        </div>
      )}
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
    let request: AbortController | undefined;
    // The list keeps whatever it printed while a refresh is in flight, so a
    // row moves only once the server has actually re-answered. A new read
    // replaces one still in flight rather than racing it.
    const load = () => {
      request?.abort();
      const controller = new AbortController();
      request = controller;
      getAgents(controller.signal)
        .then((agents) => {
          if (controller.signal.aborted) return;
          setAgents(agents);
          setError("");
        })
        .catch((reason: Error) => {
          if (!controller.signal.aborted) setError(reason.message);
        });
    };
    load();
    // The poller re-reads every endpoint on its 30-second beat, so the list
    // reads on the same beat: a printed reading is at most one beat old. A
    // hidden tab skips the beat and reads again as soon as it is shown.
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 30_000);
    const onVisible = () => {
      if (!document.hidden) load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      request?.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  // A failed refresh sits beside the last list it printed, not in its place.
  const notice = error && <p className="notice">Could not read the agents: {error}</p>;
  if (!agents) return notice || null;
  if (agents.length === 0) {
    return (
      <>
        {notice}
        <p className="state-inline">No agents are registered.</p>
      </>
    );
  }

  return (
    <>
      {notice}
      <ul className="agent-list">
        {agents.map((agent) => (
          <AgentEntry agent={agent} key={agent.name} />
        ))}
      </ul>
    </>
  );
}

/** The page the rail's agents link opens: the list and each endpoint's last reading. */
export function AgentsPage() {
  return (
    <main className="panel-scroll">
      <header className="hero">
        <p className="eyebrow">OpenEngine / Agents</p>
        <h1>Agents</h1>
        <p className="lede">
          Every registered agent, and the last reading of each URL agent's model endpoint.
        </p>
      </header>
      <AgentList />
    </main>
  );
}
