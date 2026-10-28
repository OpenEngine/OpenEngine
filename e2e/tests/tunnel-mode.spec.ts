import http from "node:http"
import type { AddressInfo } from "node:net"
import { readFileSync, writeFileSync } from "node:fs"
import { startEngine } from "../fixtures/engine.ts"
import { test, expect, GUIDE_ADDRESS, requireToken, resolveTunnelMode, type TunnelTools } from "../fixtures/tunnel.ts"

const EVERYTHING: TunnelTools = { cloudflared: true, tailscale: true, tailscaleSignedIn: true }

/** A server that answers every request, token or not: what a tunnel must never be opened to. */
async function answersAnyone() {
  const server = http.createServer((_request, response) => response.writeHead(200, { "content-type": "application/json" }).end("[]"))
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve))
  return {
    url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  }
}

test.describe("tunnel mode", { tag: ["@skip-docs"] }, () => {
  test("uses local TLS unless a real tunnel is asked for", () => {
    expect(resolveTunnelMode({}, EVERYTHING)).toBe("local")
    expect(resolveTunnelMode({ E2E_TUNNEL: "cloudflare" }, EVERYTHING)).toBe("cloudflare")
    expect(resolveTunnelMode({ E2E_TUNNEL: "tailscale" }, EVERYTHING)).toBe("tailscale")
    expect(() => resolveTunnelMode({ E2E_TUNNEL: "ngrok" }, EVERYTHING)).toThrow(/E2E_TUNNEL must be cloudflare or tailscale/)
  })

  test("never looks for tunnel tools when no real tunnel is asked for", () => {
    expect(resolveTunnelMode({}, () => {
      throw new Error("looked for cloudflared and tailscale")
    })).toBe("local")
  })

  test("refuses a real tunnel it can't open, rather than falling back to local TLS", () => {
    expect(() => resolveTunnelMode({ E2E_TUNNEL: "cloudflare" }, { ...EVERYTHING, cloudflared: false })).toThrow(/cloudflared/)
    expect(() => resolveTunnelMode({ E2E_TUNNEL: "tailscale" }, { ...EVERYTHING, tailscale: false, tailscaleSignedIn: false })).toThrow(
      /tailscale/,
    )
    expect(() => resolveTunnelMode({ E2E_TUNNEL: "tailscale" }, { ...EVERYTHING, tailscaleSignedIn: false })).toThrow(/signed in/)
    // Cloudflare's quick tunnel needs no account, and Tailscale needs no cloudflared.
    expect(resolveTunnelMode({ E2E_TUNNEL: "cloudflare" }, { ...EVERYTHING, tailscale: false, tailscaleSignedIn: false })).toBe("cloudflare")
    expect(resolveTunnelMode({ E2E_TUNNEL: "tailscale" }, { ...EVERYTHING, cloudflared: false })).toBe("tailscale")
  })

  test("refuses to open a tunnel to a daemon that answers without its token", async () => {
    const open = await answersAnyone()
    try {
      await expect(requireToken(open.url, GUIDE_ADDRESS.cloudflare)).rejects.toThrow(/without (its|a) token/)
    } finally {
      await open.close()
    }
  })

  test("opens a tunnel to a daemon that turns away a request without its token", async () => {
    // A daemon as the guide leaves the Mac mini: no GitHub login, a token, and the tunnel's address as public_url.
    const engine = await startEngine({
      agentMode: "stub",
      // At the top, where a top-level setting must go.
      beforeStart: async ({ config }) =>
        writeFileSync(config, `public_url = "https://${GUIDE_ADDRESS.cloudflare}"\n` + readFileSync(config, "utf8")),
    })
    try {
      await requireToken(engine.url, GUIDE_ADDRESS.cloudflare)
    } finally {
      await engine.stop()
    }
  })
})
