// Delete everything the last benchmark run left: the repository, its Pages site and the container
// it ran in. Run it with `npm run oe-benchmark:clean` once you have looked. Add `--logins` to also forget
// Claude's and Codex's logins, which are kept between runs.
import { removeLogins, teardown } from "../support/benchmark.ts"

console.log(await teardown())
if (process.argv.includes("--logins")) {
  await removeLogins()
  console.log("Claude's and Codex's logins removed.")
}
