/** The URL-owned part of the shell, kept apart from the mounted application so
 *  an encoded deep link can be tested without rendering the app at import. */

export type Route =
  | { kind: "login" }
  | { kind: "runs" }
  | { kind: "new-run" }
  | { kind: "run"; runId: string }
  | { kind: "graph-conversation"; runId: string; nodeId: string }
  /** What every runner's subscription has been spent on, across providers. */
  | { kind: "utilization" }
  | { kind: "new-loop" }
  | { kind: "loop"; loopId: string }
  | { kind: "chat"; threadId?: string; runId?: string };

export function routeForPath(pathname: string): Route {
  const path = pathname.replace(/\/$/, "") || "/";
  if (path === "/login") return { kind: "login" };
  if (path === "/" || path === "/runs") return { kind: "runs" };
  if (path === "/runs/new") return { kind: "new-run" };
  if (path === "/utilization") return { kind: "utilization" };
  if (path === "/loops/new") return { kind: "new-loop" };
  if (path.startsWith("/loops/"))
    return { kind: "loop", loopId: decodeURIComponent(path.slice("/loops/".length)) };
  const workflowConversation = path.match(
    /^\/runs\/([^/]+)\/conversations\/graph--([^/]+)$/,
  );
  if (workflowConversation)
    return {
      kind: "graph-conversation",
      runId: decodeURIComponent(workflowConversation[1]),
      nodeId: decodeURIComponent(workflowConversation[2]),
    };
  const stepConversation = path.match(
    /^\/runs\/([^/]+)\/conversations\/([^/]+)$/,
  );
  if (stepConversation)
    return {
      kind: "chat",
      runId: decodeURIComponent(stepConversation[1]),
      threadId: decodeURIComponent(stepConversation[2]),
    };
  if (path.startsWith("/runs/"))
    return { kind: "run", runId: decodeURIComponent(path.slice("/runs/".length)) };
  if (path.startsWith("/conversations/"))
    return { kind: "chat", threadId: decodeURIComponent(path.slice("/conversations/".length)) };
  return { kind: "chat" };
}
