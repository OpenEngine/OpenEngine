import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** Build the client every server in this run will serve.
 *
 *  The Python process serves Vite's output from inside its own package, so a
 *  stale `src/engine/apps/web/client` would mean the browser tier silently
 *  testing the previous commit's interface. Built once
 *  here rather than by each test, and unconditionally rather than by comparing
 *  timestamps: "did I need to rebuild?" is exactly the question this exists to
 *  stop anybody having to ask. */
export default function build() {
  execFileSync("npm", ["run", "build"], { cwd: WEB, stdio: "inherit" });
}
