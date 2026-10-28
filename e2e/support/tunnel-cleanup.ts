import { randomUUID } from "node:crypto"
import { execFileSync } from "node:child_process"
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import path from "node:path"

/** Recorded before exposing a daemon, so teardown can clean up after a worker dies. */
export function recordTunnel(resource: { pid: number } | { tailscale: true }) {
  const directory = process.env.E2E_TUNNEL_CLEANUP
  if (!directory) throw new Error("Missing tunnel cleanup directory")
  const file = path.join(directory, `${process.pid}-${randomUUID()}.json`)
  writeFileSync(file, JSON.stringify(resource))
  return () => rmSync(file, { force: true })
}

export default function stopTunnels() {
  const directory = process.env.E2E_TUNNEL_CLEANUP
  if (!directory || !existsSync(directory)) return
  const errors: unknown[] = []
  try {
    for (const file of readdirSync(directory)) {
      try {
        const resource = JSON.parse(readFileSync(path.join(directory, file), "utf8"))
        if (resource.tailscale) {
          execFileSync("tailscale", ["serve", "reset"], { timeout: 30_000, stdio: "pipe" })
        } else {
          try { process.kill(resource.pid, "SIGTERM") } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error
          }
        }
      } catch (error) { errors.push(error) }
    }
  } finally { rmSync(directory, { recursive: true, force: true }) }
  if (errors.length) throw new AggregateError(errors, "Could not clean up all tunnel resources")
}
