import { execFileSync } from "node:child_process"

/**
 * A free loopback port, found synchronously because Playwright reads its config
 * synchronously. (Written with `process.stdout.write`: Playwright's workers set
 * `FORCE_COLOR`, which makes `console.log` colour a number.) Racy in principle (it is released before the server binds it),
 * which a test run can live with.
 */
export function freePort(): number {
  const script = "const s=require('net').createServer().listen(0,'127.0.0.1',()=>{process.stdout.write(String(s.address().port));s.close()})"
  return Number(execFileSync(process.execPath, ["-e", script], { encoding: "utf8" }).trim())
}
