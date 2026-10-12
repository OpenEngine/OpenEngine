// node-pty's prebuilt spawn-helper ships without its executable bit, so every
// spawn fails with "posix_spawnp failed". Put the bit back after install.
import { chmodSync, existsSync, readdirSync } from "node:fs"
import path from "node:path"

const prebuilds = path.join(import.meta.dirname, "../node_modules/node-pty/prebuilds")
for (const platform of existsSync(prebuilds) ? readdirSync(prebuilds) : []) {
  const helper = path.join(prebuilds, platform, "spawn-helper")
  if (existsSync(helper)) chmodSync(helper, 0o755)
}
