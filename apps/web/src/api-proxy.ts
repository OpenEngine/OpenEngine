/** What the Vite dev server forwards to the API, and where.
 *
 *  Configurable because the two halves are started by `engine-dev`, which
 *  takes a free port for the Python half when the usual one is held by
 *  something else -- a proxy pinned to 8000 would then quietly reach the other
 *  server. The default is the port `engine-web` serves on, so running the two
 *  halves by hand needs nothing set.
 *
 *  Kept here rather than written out in `vite.config.ts` so the list of
 *  forwarded prefixes is somewhere a test can read. An unforwarded prefix is a
 *  bad failure: Vite answers a path it does not proxy with `index.html` and a
 *  200, so the client gets a page where it asked for JSON and reports a parse
 *  error rather than a 404. */

export const DEFAULT_API_URL = "http://localhost:8000";

/** Every prefix the application serves that is not the client itself.
 *
 *  `/graph` is the `[BETA]` half: the graph engine's own control surface, which
 *  the API mounts beside its own `/api` because both servers call their runs
 *  `/api/runs`. Two prefixes on the server, so two here. */
export const PROXIED_PREFIXES = ["/api", "/graph"] as const;

export function apiProxyTarget(
  environment: Record<string, string | undefined> = {},
): string {
  return environment.ENGINE_API_URL?.trim() || DEFAULT_API_URL;
}

/** The `server.proxy` block `vite.config.ts` hands Vite. */
export function apiProxy(
  environment: Record<string, string | undefined> = {},
): Record<string, { target: string; changeOrigin: boolean }> {
  const target = apiProxyTarget(environment);
  return Object.fromEntries(
    // `changeOrigin: false` keeps the browser's Host header, which Vite
    // otherwise rewrites to the proxy target. The API's CSRF guard accepts an
    // Origin only when it matches localhost or the request's own host, so a
    // rewritten Host turns every mutating call made under the tailnet name into
    // a 403.
    PROXIED_PREFIXES.map((prefix) => [prefix, { target, changeOrigin: false }]),
  );
}
