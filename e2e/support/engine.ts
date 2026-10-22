import { createHash } from "node:crypto"
import { mkdirSync, renameSync, symlinkSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")

/** The `oe` CLI the guides drive: `ENGINE_BIN`, else the repository's uv environment. */
export const engineBin = process.env.ENGINE_BIN ?? path.join(repoRoot, ".venv/bin/oe")

/**
 * A directory holding only `oe`, linked to `engineBin`, to put first on a
 * session's PATH: guides type plain `oe` and must get this checkout's,
 * not one installed elsewhere, without the rest of the virtualenv shadowing
 * the host's tools. One per engine binary, so repeated calls reuse it.
 */
export function engineBinDir(): string {
  const dir = path.join(os.tmpdir(), `openengine-e2e-bin-${createHash("sha256").update(engineBin).digest("hex").slice(0, 12)}`)
  mkdirSync(dir, { recursive: true })
  // Link under a name of our own and rename it into place: rename replaces
  // atomically, so parallel workers never see the link missing or collide.
  const temporary = path.join(dir, `.engine-${process.pid}-${Date.now()}`)
  symlinkSync(engineBin, temporary)
  renameSync(temporary, path.join(dir, "oe"))
  return dir
}
