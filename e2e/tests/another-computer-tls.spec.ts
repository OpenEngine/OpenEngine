import { test as base, expect } from "../fixtures/engine.ts"
import { MAC_MINI, MAC_MINI_PATTERN } from "./macMini"

// Sessions without SSL_CERT_FILE, so nothing trusts the proxy's certificate
// unless a command is given it. Its path stays at hand as MAC_MINI_CERTIFICATE.
const test = base.extend({
  terminalEnv: async ({ terminalEnv }, use) => {
    const { SSL_CERT_FILE, ...env } = terminalEnv
    expect(SSL_CERT_FILE).toBeTruthy()
    await use({ ...env, MAC_MINI_CERTIFICATE: SSL_CERT_FILE! })
  },
})

test.use({ anotherComputerAt: MAC_MINI })

/** `engine backends list --check --json`, prefixed with `env`, and the other computer's health. */
async function macMiniHealth(terminal: { run(command: string): Promise<{ output: string }> }, env = "") {
  const { output } = await terminal.run(`${env}engine backends list --check --json`)
  const { backends } = JSON.parse(output) as { backends: { name: string; health: string }[] }
  return backends.find((backend) => backend.name === "mac-mini")?.health
}

test.describe("another computer over https", { tag: ["@skip-docs"] }, () => {
  test("is refused on its certificate unless the run's certificate is trusted", async ({ terminal }) => {
    expect((await terminal.run("echo ${SSL_CERT_FILE-unset}")).output).toBe("unset")
    await terminal.run(`engine backend add mac-mini https://${MAC_MINI}`)

    const untrusted = await macMiniHealth(terminal)
    expect(untrusted).toMatch(new RegExp(`^down: cannot reach backend mac-mini at https://${MAC_MINI_PATTERN}: `))
    expect(untrusted).toMatch(/CERTIFICATE_VERIFY_FAILED/)

    // The same command, trusting the certificate, reaches it: only the certificate stood in the way.
    expect(await macMiniHealth(terminal, 'SSL_CERT_FILE="$MAC_MINI_CERTIFICATE" ')).toMatch(/^up\b/)
  })
})
