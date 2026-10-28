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

import { execFileSync } from "node:child_process"
import { randomBytes } from "node:crypto"
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs"
import http from "node:http"
import os from "node:os"
import path from "node:path"
import { startEngine, startProxy, type Engine } from "./engine.ts"
import { Terminal } from "./terminal.ts"
import { recordTunnel } from "../support/tunnel-cleanup.ts"
import { freePort } from "../support/freePort.ts"
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

function detectTools(): TunnelTools {
  const installed = (tool: string) => {
    try { execFileSync(tool, ["--version"], { stdio: "ignore" }); return true } catch { return false }
  }
  const tailscale = installed("tailscale")
  let tailscaleSignedIn = false
  if (tailscale) {
    try {
      const status = JSON.parse(execFileSync("tailscale", ["status", "--json"], { encoding: "utf8" }))
      tailscaleSignedIn = status.BackendState === "Running"
    } catch { /* resolveTunnelMode explains what is missing. */ }
  }
  return { cloudflared: installed("cloudflared"), tailscale, tailscaleSignedIn }
}

/**
 * `E2E_TUNNEL`: unset for local TLS; `cloudflare` or `tailscale` for a real
 * tunnel, refused where this computer can't open one.
 */
export function resolveTunnelMode(
  env: Record<string, string | undefined> = process.env,
  detect?: TunnelTools | (() => TunnelTools),
): TunnelMode {
  const mode = env.E2E_TUNNEL
  if (!mode) return "local"
  if (mode !== "cloudflare" && mode !== "tailscale") throw new Error("E2E_TUNNEL must be cloudflare or tailscale")
  const found = typeof detect === "function" ? detect() : detect ?? detectTools()
  if (mode === "cloudflare" && !found.cloudflared) throw new Error("E2E_TUNNEL=cloudflare needs cloudflared installed")
  if (mode === "tailscale" && !found.tailscale) throw new Error("E2E_TUNNEL=tailscale needs tailscale installed")
  if (mode === "tailscale" && !found.tailscaleSignedIn) throw new Error("tailscale must be signed in to a tailnet")
  return mode
}

/**
 * Resolve if the daemon at `url`, asked for /api/v1 as `host` (the tunnel's
 * address) and without a token, turns the request away; throw otherwise.
 */
export async function requireToken(url: string, host: string): Promise<void> {
  for (const headers of [{ host }, { host: "127.0.0.1", "x-forwarded-for": "203.0.113.7" }] as Record<string, string>[]) {
    const status = await new Promise<number>((resolve, reject) => {
      const request = http.get(`${url}/api/v1/graphs`, { headers, timeout: 10_000 }, (answer) => {
        answer.resume()
        resolve(answer.statusCode ?? 0)
      })
      request.on("error", reject)
      request.on("timeout", () => request.destroy(new Error("Token preflight timed out")))
    })
    if (status !== 401) throw new Error(`Refusing to open a tunnel: daemon answers without its token (HTTP ${status})`)
  }
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
  tunnelledComputer: async ({ tunnel, engine, context }, use, testInfo) => {
    if (!tunnel) {
      await use(undefined)
      return
    }
    const mode = resolveTunnelMode()
    const kind = tunnel
    const real = mode !== "local"
    if (real) testInfo.setTimeout(180_000)
    const token = randomBytes(32).toString("hex")
    let host = GUIDE_ADDRESS[kind]
    const port = real ? freePort() : 0
    const start = () => startEngine({
      agentMode: engine.agentMode, token, port,
      beforeStart: async ({ config }) => writeFileSync(config, `public_url = "https://${host}"\n` + readFileSync(config, "utf8")),
    })
    let other = await start()
    const home = mkdtempSync(path.join(os.tmpdir(), "e2e-tunnel-home-"))
    let tunnelTerminal: Terminal | undefined
    let serving = false
    let forgetTunnel: (() => void) | undefined
    try {
      // Check the real HTTP boundary before any public forwarding starts.
      await requireToken(other.url, host)
      if (real) {
        await other.stop()
        if (mode === "cloudflare") {
          tunnelTerminal = await Terminal.open(await context.newPage(), { env: { HOME: home }, cols: 160, rows: 60 })
          await tunnelTerminal.type(`exec cloudflared tunnel --url http://127.0.0.1:${port}`)
          const pid = await tunnelTerminal.page.evaluate(() => window.__term!.pid!)
          forgetTunnel = recordTunnel({ pid })
          await tunnelTerminal.press("Enter")
          await tunnelTerminal.waitForText(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/, { timeout: 90_000 })
          host = /https:\/\/([a-z0-9-]+\.trycloudflare\.com)/.exec(await tunnelTerminal.text())![1]!
          await tunnelTerminal.waitForText(/Registered tunnel connection/, { timeout: 90_000 })
        } else {
          // Do not overwrite a developer's existing Serve configuration.
          const config = JSON.parse(execFileSync("tailscale", ["serve", "status", "--json"], { encoding: "utf8" }))
          if (config && Object.keys(config).length) throw new Error("tailscale serve is already configured; use a dedicated test machine")
          const status = JSON.parse(execFileSync("tailscale", ["status", "--json"], { encoding: "utf8" }))
          host = status.Self.DNSName.replace(/\.$/, "")
          forgetTunnel = recordTunnel({ tailscale: true })
          serving = true
          execFileSync("tailscale", ["serve", "--bg", `http://127.0.0.1:${port}`], { stdio: "pipe", timeout: 30_000 })
        }
        other = await start()
        await requireToken(other.url, host)
        const deadline = Date.now() + 45_000
        let ready = false
        while (Date.now() < deadline) {
          try {
            const response = await fetch(`https://${host}/api/v1/graphs`, {
              headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5_000),
            })
            await response.body?.cancel()
            if (response.status === 200) { ready = true; break }
          } catch { /* DNS and the tunnel connection may still be coming up. */ }
          await new Promise((resolve) => setTimeout(resolve, 500))
        }
        if (!ready) throw new Error(`The ${mode} tunnel never reached the token-protected daemon`)
      }
      await use({ ...other, kind, real, host, publicUrl: `https://${host}`, token })
    } finally {
      try {
        if (tunnelTerminal) await tunnelTerminal.page.close()
        if (serving) execFileSync("tailscale", ["serve", "reset"], { stdio: "pipe", timeout: 30_000 })
        forgetTunnel?.()
      } finally {
        await other.stop()
        rmSync(home, { recursive: true, force: true })
      }
    }
  },
  terminalEnv: async ({ engine, tunnelledComputer }, use) => {
    const computer = tunnelledComputer
    if (!computer) { await use(engine.env); return }
    if (computer.real) { await use({ ...engine.env, E2E_GUIDE_ADDRESS: GUIDE_ADDRESS[computer.kind] }); return }
    const proxy = await startProxy(computer.host, computer.url, true)
    try {
      await use({ ...engine.env, http_proxy: proxy.url, https_proxy: proxy.url,
        no_proxy: "127.0.0.1,localhost", SSL_CERT_FILE: proxy.certificate })
    } finally { await proxy.close() }
  },
})
