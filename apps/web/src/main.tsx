import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ChatApp } from "./app";
import { NewWorkflowPage, RunDetailPage, RunsPage } from "./runs";
import "./styles.css";

function App() {
  const path = window.location.pathname.replace(/\/$/, "") || "/";
  if (path === "/" || path === "/runs") return <RunsPage />;
  if (path === "/runs/new") return <NewWorkflowPage />;
  const workflowConversation = path.match(
    /^\/runs\/([^/]+)\/conversations\/([^/]+)$/,
  );
  if (workflowConversation) {
    return (
      <ChatApp
        workflowRunId={decodeURIComponent(workflowConversation[1])}
        initialThreadId={decodeURIComponent(workflowConversation[2])}
      />
    );
  }
  if (path.startsWith("/runs/")) {
    return <RunDetailPage runId={decodeURIComponent(path.slice("/runs/".length))} />;
  }
  if (path.startsWith("/conversations/")) {
    return <ChatApp initialThreadId={decodeURIComponent(path.slice("/conversations/".length))} />;
  }
  return <ChatApp />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
