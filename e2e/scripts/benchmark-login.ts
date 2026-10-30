// Sign GitHub, Claude and Codex in for the benchmark, asking only for the ones that aren't. Claude and Codex are signed in
// inside the benchmark's image: each prints a link to open in a browser and asks for the code it gives back (Codex's works
// from a machine with no browser too). Their logins are kept in Docker volumes, so later runs find them; they are never
// copied to or from this computer. GitHub's is `gh`'s own config directory (`GH_CONFIG_DIR`, or the default one), with the
// token kept in a file, since the benchmark's container can't read the system keyring. Run it with `npm run oe-benchmark:login`.
import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { BENCH_IMAGE, ensureImage, LOGIN_VOLUMES } from "../support/benchmark.ts"

const ghConfig = process.env.GH_CONFIG_DIR ?? path.join(os.homedir(), ".config", "gh")
const SCOPES = "repo,workflow,delete_repo"

const gh = (...args: string[]) => spawnSync("gh", args, { stdio: "inherit" })
const ghQuiet = (...args: string[]) => spawnSync("gh", args, { stdio: "ignore" })

/** Stop the script if a sign-in was abandoned or failed, so the next run doesn't fail minutes in. */
const ok = (status: number | null) => {
  if (status !== 0) process.exit(status ?? 1)
}

const tokenInFile = () => {
  const hosts = path.join(ghConfig, "hosts.yml")
  return existsSync(hosts) && /^\s*oauth_token:/m.test(readFileSync(hosts, "utf8"))
}

if (ghQuiet("api", "user").status === 0 && tokenInFile()) {
  console.log("GitHub: already signed in.")
} else if (ghQuiet("api", "user").status === 0) {
  console.log(`\nGitHub: signed in, but the token is in the system keyring, which the benchmark's container can't read. Moving it into ${ghConfig}.\n`)
  ok(gh("auth", "refresh", "-h", "github.com", "-s", SCOPES, "--insecure-storage").status)
} else {
  console.log(`\nGitHub: not signed in (${ghConfig}). Sign in as the account the benchmark should use.\n`)
  ok(gh("auth", "login", "-h", "github.com", "-p", "https", "-s", SCOPES, "--insecure-storage").status)
}

// `gh` can report success without keeping the token where asked, so look instead of trusting it.
if (!tokenInFile()) {
  console.error(
    `\nGitHub: the token is still not in ${path.join(ghConfig, "hosts.yml")}. Sign out and in again to replace it:\n` +
      `  GH_CONFIG_DIR=${ghConfig} gh auth logout\n  GH_CONFIG_DIR=${ghConfig} npm run oe-benchmark:login`,
  )
  process.exit(1)
}

await ensureImage()
const volumes = ["-v", `${LOGIN_VOLUMES.claude}:/home/node/.claude`, "-v", `${LOGIN_VOLUMES.codex}:/home/node/.codex`]

for (const [agent, file, command] of [
  ["Claude", "/home/node/.claude/.credentials.json", "claude auth login"],
  ["Codex", "/home/node/.codex/auth.json", "codex login --device-auth"],
] as const) {
  const signedIn = spawnSync("docker", ["run", "--rm", ...volumes, BENCH_IMAGE, "test", "-s", file], { stdio: "ignore" }).status === 0
  if (signedIn) {
    console.log(`${agent}: already signed in.`)
    continue
  }
  console.log(`\nSigning ${agent} in: ${command}\n`)
  ok(spawnSync("docker", ["run", "-it", "--rm", ...volumes, BENCH_IMAGE, "sh", "-c", command], { stdio: "inherit" }).status)
}
console.log("\nAll signed in. The benchmark will use these logins until they lapse.")
