// Sign Claude and Codex in once, inside the benchmark's image. Each prints a link to open in a browser and asks for
// the code it gives back (Codex's works from a machine with no browser too). The logins are kept in Docker volumes,
// so later runs find them; they are never copied to or from this computer. Run it with `npm run oe-benchmark:login`.
import { spawnSync } from "node:child_process"
import { BENCH_IMAGE, ensureImage, LOGIN_VOLUMES } from "../support/benchmark.ts"

await ensureImage()
for (const [agent, command] of [
  ["Claude", "claude auth login"],
  ["Codex", "codex login --device-auth"],
] as const) {
  console.log(`\nSigning ${agent} in: ${command}\n`)
  const { status } = spawnSync(
    "docker",
    ["run", "-it", "--rm", "-v", `${LOGIN_VOLUMES.claude}:/home/node/.claude`, "-v", `${LOGIN_VOLUMES.codex}:/home/node/.codex`, BENCH_IMAGE, "sh", "-c", command],
    { stdio: "inherit" },
  )
  if (status !== 0) process.exit(status ?? 1)
}
console.log("\nBoth are signed in. The benchmark will use these logins until they lapse.")
