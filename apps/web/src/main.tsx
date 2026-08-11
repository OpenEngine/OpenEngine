import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { api, type EngineConfig } from "./api";
import { ChatSidebar, ChatThread } from "./chat";
import { EngineRuntimeProvider } from "./runtime";
import "./styles.css";

function App() {
  const [config, setConfig] = useState<EngineConfig | null>(null);
  const [error, setError] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runner, setRunner] = useState("");

  useEffect(() => {
    api<EngineConfig>("/api/config")
      .then((value) => {
        setConfig(value);
        setAgentId(value.defaultAgent);
        setRunner(value.defaultRunner);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  if (error) return <main className="fatal">Could not connect to engine: {error}</main>;
  if (!config || !agentId || !runner) return <main className="loading">Starting engine…</main>;

  return (
    <EngineRuntimeProvider defaults={{ agentId, runner }}>
      <div className="app-shell">
        <ChatSidebar />
        <main className="chat-panel">
          <header className="chat-header">
            <div>
              <span className="eyebrow">NEW CHAT DEFAULTS</span>
              <p>Choose what starts the next conversation and which runner answers.</p>
            </div>
            <label>
              <span>Agent</span>
              <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                {config.agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.id} — {agent.description}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Runner</span>
              <select value={runner} onChange={(event) => setRunner(event.target.value)}>
                {config.runners.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.id}
                  </option>
                ))}
              </select>
            </label>
          </header>
          <ChatThread />
        </main>
      </div>
    </EngineRuntimeProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
