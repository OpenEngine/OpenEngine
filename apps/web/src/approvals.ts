/** What each conversation's run is currently waiting on, or last waited on.
 *
 *  An approval arrives interleaved with message content on the run stream, but
 *  it is not message content: it is the state of the turn, it outlives the
 *  connection that carried it, and it has to be readable from a component that
 *  is nowhere near the code reading the stream. So it is kept here, beside
 *  assistant-ui's message state rather than inside it, and read through
 *  `useSyncExternalStore` so a card re-renders the moment a snapshot lands.
 *
 *  Keyed by thread because runs are: several conversations stream at once, and
 *  a pause in one must not appear under another.
 */

import { useCallback, useSyncExternalStore } from "react";

import type { ApiApproval } from "./api";

const current = new Map<string, ApiApproval | null>();
const listeners = new Map<string, Set<() => void>>();

/** Record the latest snapshot for a thread, pending or resolved. `null` clears
 *  it -- what a new run does, so the last run's answered request does not hang
 *  around under a question nobody asked yet. */
export function publishApproval(threadId: string, approval: ApiApproval | null): void {
  current.set(threadId, approval);
  for (const listener of listeners.get(threadId) ?? []) listener();
}

export function readApproval(threadId: string): ApiApproval | null {
  return current.get(threadId) ?? null;
}

function subscribeApproval(threadId: string, listener: () => void): () => void {
  const existing = listeners.get(threadId) ?? new Set<() => void>();
  existing.add(listener);
  listeners.set(threadId, existing);
  return () => {
    existing.delete(listener);
    if (!existing.size) listeners.delete(threadId);
  };
}

/** The conversation's current approval snapshot, or null when it has none. */
export function useApproval(threadId: string | null | undefined): ApiApproval | null {
  const subscribe = useCallback(
    (listener: () => void) =>
      threadId ? subscribeApproval(threadId, listener) : () => {},
    [threadId],
  );
  // Returns the stored reference rather than a fresh object, which is what
  // lets React tell "nothing changed" from "a new snapshot arrived".
  const snapshot = useCallback(
    () => (threadId ? readApproval(threadId) : null),
    [threadId],
  );
  return useSyncExternalStore(subscribe, snapshot);
}
