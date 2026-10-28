import type { Terminal } from "../fixtures/terminal.ts"

/** Start in the second terminal, ignoring any existing cloudflared configuration. */
export async function launchQuickTunnel(terminal: Terminal, port: number, {
  beforeLaunch, onAddress,
}: { beforeLaunch?: () => Promise<void>; onAddress?: (url: string) => Promise<void> } = {}): Promise<string> {
  await terminal.type(`exec env HOME="$(mktemp -d)" cloudflared tunnel --url http://127.0.0.1:${port}`)
  await beforeLaunch?.()
  await terminal.press("Enter")
  await terminal.waitForText(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/, { timeout: 90_000 })
  const url = /https:\/\/[a-z0-9-]+\.trycloudflare\.com/.exec(await terminal.text())![0]
  await onAddress?.(url)
  await terminal.waitForText(/Registered tunnel connection/, { timeout: 90_000 })
  return url
}
