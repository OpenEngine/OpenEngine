// The Mac mini of the reach-from-anywhere guide: a daemon of its own, with a
// token of its own, reached at an https address a tunnel gives it.
//
// By default there is no tunnel: the address is answered on this computer with
// local TLS (see startProxy in engine.ts), so the suite and the guides never
// depend on Cloudflare or Tailscale. E2E_TUNNEL asks for a real one, the way
// E2E_AGENT=real asks for real agents:
//
//   E2E_TUNNEL=cloudflare  a Cloudflare quick tunnel (`cloudflared tunnel --url ...`), no account needed
//   E2E_TUNNEL=tailscale   `tailscale serve` on this computer, which must be signed in to a tailnet
//
// and refuses to start where it can't, rather than quietly using local TLS.
// The terminal page draws a real tunnel's changing address as the guide's
// address (GUIDE_ADDRESS), so the guides come out the same either way.
//
// A tunnelled daemon runs agents and shell commands for whoever reaches it, so
// it is never opened to the internet unless it turns away a request without
// its token (requireToken).

import type { Engine } from "./engine.ts"
import { test as engineTest } from "./engine.ts"

export { expect } from "./engine.ts"

/** Which tunnel a guide section is about. */
export type TunnelKind = "cloudflare" | "tailscale"

/** `local`: local TLS, no tunnel (the default); otherwise the real tunnel E2E_TUNNEL names. */
export type TunnelMode = "local" | TunnelKind

/** What this computer has for opening a real tunnel. */
export type TunnelTools = { cloudflared: boolean; tailscale: boolean; tailscaleSignedIn: boolean }

/** The address each guide shows: what local TLS answers at, and what the page draws a real tunnel's address as. */
export const GUIDE_ADDRESS: Record<TunnelKind, string> = {
  cloudflare: "openengine.example.com",
  tailscale: "mac-mini.tail1234.ts.net",
}

function notYet(what: string): Error {
  return new Error(`${what} is not implemented yet (#881)`)
}

/**
 * `E2E_TUNNEL`: unset for local TLS; `cloudflare` or `tailscale` for a real
 * tunnel, refused where this computer can't open one.
 */
export function resolveTunnelMode(
  env: Record<string, string | undefined> = process.env,
  detect?: TunnelTools | (() => TunnelTools),
): TunnelMode {
  void env
  void detect
  throw notYet("resolveTunnelMode")
}

/**
 * Resolve if the daemon at `url`, asked for /api/v1 as `host` (the tunnel's
 * address) and without a token, turns the request away; throw otherwise.
 */
export async function requireToken(url: string, host: string): Promise<void> {
  void url
  void host
  throw notYet("requireToken")
}

/** The Mac mini behind a tunnel. */
export type TunnelledComputer = Engine & {
  kind: TunnelKind
  /** Whether a real tunnel reaches it (E2E_TUNNEL), rather than local TLS. */
  real: boolean
  /** The host name the tunnel gives it: GUIDE_ADDRESS[kind] with local TLS, the tunnel's own otherwise. */
  host: string
  /** `https://` and `host`: what the reader types, and the daemon's `public_url`. */
  publicUrl: string
  /** The Mac mini's own token, which this computer's ENGINE_SERVICE_TOKEN is not. */
  token: string
}

export const test = engineTest.extend<{
  tunnel: TunnelKind | undefined
  tunnelledComputer: TunnelledComputer | undefined
}>({
  /** Which tunnel reaches the Mac mini; set it with `test.use` to get `tunnelledComputer`. */
  tunnel: [undefined, { option: true }],
  tunnelledComputer: async ({ tunnel }, use) => {
    if (!tunnel) {
      await use(undefined)
      return
    }
    throw notYet("the tunnelledComputer fixture")
  },
})
