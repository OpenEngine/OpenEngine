import { execFileSync } from "node:child_process"

/**
 * A free loopback port, found synchronously because Playwright reads its config
 * synchronously. Racy in principle (it is released before the server binds it),
 * which a test run can live with.
 */
export function freePort(): number {
  const script = "const s=require('net').createServer().listen(0,'127.0.0.1',()=>{console.log(s.address().port);s.close()})"
  return Number(execFileSync(process.execPath, ["-e", script], { encoding: "utf8" }).trim())
}
