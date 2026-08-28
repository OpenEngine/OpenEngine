/** Where the Vite dev server sends `/api`.
 *
 *  Configurable because the two halves are started by `engine-dev`, which
 *  takes a free port for the Python half when the usual one is held by
 *  something else -- a proxy pinned to 8000 would then quietly reach the other
 *  server. The default is the port `engine-web` serves on, so running the two
 *  halves by hand needs nothing set. */

export const DEFAULT_API_URL = "http://localhost:8000";

export function apiProxyTarget(
  environment: Record<string, string | undefined> = {},
): string {
  return environment.ENGINE_API_URL?.trim() || DEFAULT_API_URL;
}
