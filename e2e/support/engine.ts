import path from "node:path"
import { fileURLToPath } from "node:url"

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")

/** The `engine` CLI the guides drive: `ENGINE_BIN`, else the repository's uv environment. */
export const engineBin = process.env.ENGINE_BIN ?? path.join(repoRoot, ".venv/bin/engine")
