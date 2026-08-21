import { useEffect, useState } from "react";

import {
  api,
  attachWorkspace,
  detachWorkspace,
  type ApiThread,
} from "./api";

export type WorkspaceInfo = Pick<
  ApiThread,
  "workspaceRoot" | "workspaceRef" | "workspaceAttached"
>;

/** A conversation's checkout, shared by chat and workflow surfaces. */
export function WorkspaceControl({
  threadId,
  initial,
}: {
  threadId: string;
  initial?: Partial<WorkspaceInfo>;
}) {
  const [fetched, setFetched] = useState<ApiThread>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    setFetched(undefined);
    setError(undefined);
    let current = true;
    void api<ApiThread>(`/api/threads/${threadId}`)
      .then((thread) => {
        if (current) setFetched(thread);
      })
      .catch(() => {});
    return () => {
      current = false;
    };
  }, [threadId]);

  const workspace = fetched ?? initial ?? {};
  const attached =
    workspace.workspaceAttached ?? Boolean(workspace.workspaceRoot);

  async function toggle() {
    setBusy(true);
    setError(undefined);
    try {
      setFetched(
        await (attached ? detachWorkspace : attachWorkspace)(threadId),
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workspace-control">
      {attached ? (
        <>
          <span className="micro">Working in</span>
          <code className="dock-path">cd {workspace.workspaceRoot}</code>
        </>
      ) : workspace.workspaceRef ? (
        <>
          <span className="micro">Detached — the work is on</span>
          <code className="dock-path">git checkout {workspace.workspaceRef}</code>
        </>
      ) : (
        <span className="micro">
          No worktree — attach one to give this conversation somewhere to work
        </span>
      )}
      <button
        type="button"
        className="link-flame"
        onClick={() => void toggle()}
        disabled={busy}
      >
        {busy
          ? "Working…"
          : attached
            ? "Detach"
            : workspace.workspaceRef
              ? "Reattach"
              : "Attach"}
      </button>
      {error && (
        <p className="notice dock-error">
          {error.split(/`([^`]+)`/).map((part, index) =>
            index % 2 ? <code key={index}>{part}</code> : part,
          )}
        </p>
      )}
    </div>
  );
}
