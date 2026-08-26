/** The URL-owned part of the shell, kept apart from the mounted application so
 *  an encoded deep link can be tested without rendering the app at import. */

export type Route =
  | { kind: "runs" }
  | { kind: "new-run" }
  | { kind: "run"; runId: string }
  | { kind: "project"; projectId: string }
  | { kind: "milestone"; projectId: string; milestoneId: string }
  /** `plan` is the same chat page, opened on the planning agent and always on
   *  a new conversation. */
  | { kind: "chat"; threadId?: string; runId?: string; plan?: boolean };

export function routeForPath(pathname: string): Route {
  const path = pathname.replace(/\/$/, "") || "/";
  if (path === "/" || path === "/runs") return { kind: "runs" };
  if (path === "/runs/new") return { kind: "new-run" };
  if (path === "/plan") return { kind: "chat", plan: true };
  const projectMilestones = path.match(/^\/projects\/([^/]+)\/milestones$/);
  if (projectMilestones)
    return { kind: "project", projectId: decodeURIComponent(projectMilestones[1]) };
  const milestoneDetails = path.match(/^\/projects\/([^/]+)\/milestones\/([^/]+)$/);
  if (milestoneDetails)
    return {
      kind: "milestone",
      projectId: decodeURIComponent(milestoneDetails[1]),
      milestoneId: decodeURIComponent(milestoneDetails[2]),
    };
  const workflowConversation = path.match(
    /^\/runs\/([^/]+)\/conversations\/([^/]+)$/,
  );
  if (workflowConversation)
    return {
      kind: "chat",
      runId: decodeURIComponent(workflowConversation[1]),
      threadId: decodeURIComponent(workflowConversation[2]),
    };
  if (path.startsWith("/runs/"))
    return { kind: "run", runId: decodeURIComponent(path.slice("/runs/".length)) };
  if (path.startsWith("/conversations/"))
    return { kind: "chat", threadId: decodeURIComponent(path.slice("/conversations/".length)) };
  return { kind: "chat" };
}
