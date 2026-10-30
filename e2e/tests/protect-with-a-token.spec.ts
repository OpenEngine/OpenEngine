import { readFileSync } from "node:fs"
import path from "node:path"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test as base, expect, startEngine } from "../fixtures/engine.ts"
import { Terminal } from "../fixtures/terminal.ts"
import { engineBin } from "../support/engine.ts"
import { show, step } from "../support/show.ts"
import { anotherComputerCategory } from "./docCategories"
import { MAC_MINI } from "./macMini"

// Separate from local's credential, so falling back to local cannot grant access.
const TOKEN = "example-only-8a71f204d6c539be072a19f5e4c083bd"
const test = base.extend({
  anotherComputer: async ({ engine }, use) => {
    const other = await startEngine({ agentMode: engine.agentMode, token: TOKEN })
    try { await use({ ...other, address: MAC_MINI }) }
    finally { await other.stop() }
  },
})
test.use({ terminalSession: { cols: 120, rows: 30 } })

test.describe(anotherComputerCategory, () => {
  test.describe(withDocMeta("Protect Your OpenEngine with a Token", {
    description: "Give your other computer a secret token, then sign in with it from a browser or use it from this computer's terminal.",
    sidebar_position: 2,
    slug: "/guides/other-computers/protect-with-a-token",
  }), () => {
    test("Protect the other computer and recover from a missing or wrong token", async ({ engine, anotherComputer, terminal, context }, testInfo) => {
      const other = anotherComputer!
      const remotePage = await context.newPage()
      const remote = await Terminal.open(remotePage, {
        cwd: other.repository, cols: 120, rows: 30,
        env: { ...other.daemonEnv, ENGINE_SERVICE_TOKEN: "", ENGINE_PORT: new URL(other.url).port,
          PATH: `${path.dirname(engineBin)}:${process.env.PATH}` },
      })
      const showRow = async (filename: string, session: Terminal, row: RegExp, label: string) =>
        show(filename, session, testInfo, await session.row(row), label)
      const clear = () => test.step("[nodoc] Clear the local screen", async () => void await terminal.run("clear"))
      const status = async (token?: string) => (await fetch(`${other.url}/api/v1/graphs`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })).status
      const saved = () => JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
      let running = false
      try {
        await step(`Anyone who can reach your OpenEngine address could otherwise use it. A **token** is a secret password that OpenEngine asks for before doing work. Give the OpenEngine on your other computer a token so only people with that token can use it.

Once it has a token, OpenEngine asks for it everywhere, even in a browser on the other computer itself. In a browser, you sign in once with the token. In a terminal, the \`oe\` command sends the token for you. If you never set a token, OpenEngine stays open to programs on its own computer, as before.

OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. A **backend** is a computer where OpenEngine's background program is running: this one, or another one such as a Mac mini. The \`oe\` command is what you type in a terminal to use OpenEngine.

You'll need OpenEngine installed on both computers and a private network or tunnel that lets this computer reach the other one. Here its address is \`https://${MAC_MINI}\`; replace it with yours. Keep using that protected connection: a token does not encrypt the connection. To reach the other computer from outside your home network, see [Reach Your OpenEngine From Anywhere](./test2doc-reach-your-openengine-from-anywhere.mdx). Keep your token private and choose your own unique value instead of the example below. It must be at least 32 characters long, with no spaces; OpenEngine won't start with a shorter one. Start in a terminal on the other computer.`, async () => {})
        await test.step("[nodoc] Stop the remote program before restarting it", async () => { await other.stopServing() })
        await step(`On the other computer, type \`export ENGINE_SERVICE_TOKEN='${TOKEN}'\` and press Enter.

This stores the token in that terminal for OpenEngine to read when it starts. It lasts until you close the terminal. If OpenEngine is already running, stop it first with Ctrl+C in its terminal.`, async () => {
          await remote.run(`export ENGINE_SERVICE_TOKEN='${TOKEN}'`)
          await showRow("protect-with-a-token__other-terminal--token-set.png", remote, /export ENGINE_SERVICE_TOKEN=/, "The token is set on the other computer")
        })
        await test.step("[nodoc] Clear the remote screen", async () => void await remote.run("clear"))
        await step("In that same terminal on the other computer, type `engine-web` and press Enter.\n\nLeave this terminal open. OpenEngine starts with the token you set. If you start it again later, set the same token first. If someone manages its startup for you, ask them to put `ENGINE_SERVICE_TOKEN` in its startup environment and restart it.", async () => {
          await remote.type("engine-web")
          await remote.press("Enter")
          running = true
          // Just the startup lines: the configuration line above them names
          // a folder on this computer and settings this guide doesn't cover.
          // Taken before the requests below, which the server would log.
          const startup = (await (await remote.rows(/Started server process/, /Uvicorn running/)).boundingBox())!
          const screen = await remote.usedArea()
          await screenshot(testInfo, await remote.row(/Uvicorn running/), {
            annotation: { filename: "protect-with-a-token__other-terminal--started.png", text: "OpenEngine is running with token protection" },
            clip: { x: screen.x, y: startup.y, width: screen.width, height: startup.height + 56 },
          })
          await expect.poll(async () => { try { return await status() } catch { return 0 } }).toBe(401)
          expect(await status(TOKEN)).toBe(200)
        })
        await step(`To use OpenEngine in a browser now, open its address, \`https://${MAC_MINI}\`. Use that address on the other computer too: if you gave OpenEngine a public address in [Reach Your OpenEngine From Anywhere](./test2doc-reach-your-openengine-from-anywhere.mdx), it refuses sign-in from \`http://127.0.0.1:4364\`. Only without a public address can you sign in at \`http://127.0.0.1:4364\` on the other computer itself. Instead of your work, you see a sign-in form. Paste the token you set and choose sign in.

The browser remembers that you signed in, even after you close it. Choose Sign out when you finish on a computer other people use. If you change the token later, every browser has to sign in again.`, async () => {
          // What the browser does: refused until it signs in, then let in by its session cookie.
          expect((await fetch(`${other.url}/api/runs`)).status).toBe(401)
          const signedIn = await fetch(`${other.url}/api/auth/token/login`, {
            method: "POST", redirect: "manual",
            headers: { Origin: other.url, "Content-Type": "application/x-www-form-urlencoded" },
            body: new URLSearchParams({ token: TOKEN }),
          })
          expect(signedIn.status).toBe(303)
          const cookie = signedIn.headers.getSetCookie().find((value) => value.startsWith("engine_token_session="))!
          expect(cookie).toBeTruthy()
          expect((await fetch(`${other.url}/api/runs`, { headers: { Cookie: cookie.split(";")[0] } })).status).toBe(200)
        })
        await step(`To use the other computer from a terminal on this one, type \`export MAC_MINI_TOKEN='${TOKEN}'\` and press Enter.

Copy the same secret value you set on the other computer. \`MAC_MINI_TOKEN\` is a name for storing it in this terminal; you can choose another name. Set it again whenever you open a new terminal. Don't paste the token into a shared document or commit it with your code.`, async () => {
          await terminal.run(`export MAC_MINI_TOKEN='${TOKEN}'`)
          await showRow("protect-with-a-token__terminal--token-stored.png", terminal, /export MAC_MINI_TOKEN=/, "This terminal holds the matching token")
        })
        await clear()
        await step(`Type \`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN\` and press Enter.

Use the variable's name after \`--token-env\`, not the secret value. OpenEngine saves the name and reads its value whenever you send a command.`, async () => {
          await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN`)
          expect(saved().backends["mac-mini"].token_env).toBe("MAC_MINI_TOKEN")
          expect(JSON.stringify(saved())).not.toContain(TOKEN)
          await showRow("protect-with-a-token__terminal--backend-added.png", terminal, /^mac-mini\s+https:/, "The Mac mini is saved with your token's variable name")
        })
        await clear()
        await step(`If you already added the Mac mini by following [Use OpenEngine on Another Computer](../test2doc-other-computers/test2doc-use-openengine-on-another-computer.mdx), OpenEngine doesn't save it again. Instead, it says the Mac mini already exists, like this.`, async () => {
          await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN`, { exitCode: 1 })
          await showRow("protect-with-a-token__terminal--backend-exists.png", terminal, /already exists/, "The Mac mini is already saved")
        })
        await clear()
        await step(`In that case, type \`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN --replace\` and press Enter.

\`--replace\` updates the Mac mini you saved before, so it now uses your token's variable name. OpenEngine shows what it saved, as above.`, async () => {
          await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN --replace`)
          expect(saved().backends["mac-mini"].token_env).toBe("MAC_MINI_TOKEN")
          expect(JSON.stringify(saved())).not.toContain(TOKEN)
          await showRow("protect-with-a-token__terminal--backend-replaced.png", terminal, /^mac-mini\s+https:/, "The Mac mini now uses your token's variable name")
        })
        await clear()
        await step("Type `oe graphs list --backend mac-mini` and press Enter.\n\nThe Mac mini answers with its list of graphs. A graph is a recipe for a piece of work: its steps, which agent does each one, and in what order. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you. An empty list is fine too: the token let you in.", async () => {
          await terminal.run("oe graphs list --backend mac-mini")
          await showRow("protect-with-a-token__terminal--access-granted.png", terminal, /^NAME\s+VERSION/, "The Mac mini accepted your token")
        })
        await clear()
        await step("To see what happens in a terminal without the token, type `unset MAC_MINI_TOKEN` and press Enter.\n\nThis removes the token from this terminal. The other computer stays protected.", async () => {
          await terminal.run("unset MAC_MINI_TOKEN")
          await showRow("protect-with-a-token__terminal--token-unset.png", terminal, /unset MAC_MINI_TOKEN/, "This terminal no longer has the token")
        })
        await clear()
        await step("Type `oe graphs list --backend mac-mini` and press Enter.\n\nOpenEngine refuses the request and says the Mac mini requires a token. If you see this in a new terminal, store the matching token there again as shown next.", async () => {
          await terminal.run("oe graphs list --backend mac-mini", { exitCode: 1 })
          expect(await status()).toBe(401)
          await showRow("protect-with-a-token__terminal--missing-token.png", terminal, /requires a token/, "Without the token, the Mac mini refuses access")
        })
        await clear()
        await step(`Type \`export MAC_MINI_TOKEN='${TOKEN}'\` and press Enter to restore the matching token.`, async () => {
          await terminal.run(`export MAC_MINI_TOKEN='${TOKEN}'`)
          await showRow("protect-with-a-token__terminal--token-restored.png", terminal, /export MAC_MINI_TOKEN=/, "The matching token is restored")
        })
        await clear()
        await step("Type `oe graphs list --backend mac-mini` and press Enter.\n\nThe list appears again: you have access.", async () => {
          await terminal.run("oe graphs list --backend mac-mini")
          await showRow("protect-with-a-token__terminal--access-restored.png", terminal, /^NAME\s+VERSION/, "Access works after restoring the token")
        })
        await clear()
        await step("To see what a mistyped token does, type `export MAC_MINI_TOKEN='wrong-example-token'` and press Enter.", async () => {
          await terminal.run("export MAC_MINI_TOKEN='wrong-example-token'")
          await showRow("protect-with-a-token__terminal--wrong-token-set.png", terminal, /export MAC_MINI_TOKEN=/, "This token differs from the other computer's token")
        })
        await clear()
        await step("Type `oe graphs list --backend mac-mini` and press Enter.\n\nOpenEngine refuses this token too. The same token message can mean either a missing token or one that doesn't match. Copy the value from the other computer carefully; keep the variable name you saved with `--token-env`.", async () => {
          await terminal.run("oe graphs list --backend mac-mini", { exitCode: 1 })
          expect(await status("wrong-example-token")).toBe(401)
          await showRow("protect-with-a-token__terminal--wrong-token.png", terminal, /requires a token/, "A wrong token cannot open the Mac mini either")
        })
        await clear()
        await step(`Type \`export MAC_MINI_TOKEN='${TOKEN}'\` and press Enter to replace the wrong value with the matching token.`, async () => {
          await terminal.run(`export MAC_MINI_TOKEN='${TOKEN}'`)
          await showRow("protect-with-a-token__terminal--token-corrected.png", terminal, /export MAC_MINI_TOKEN=/, "The token matches the other computer again")
        })
        await clear()
        await step("Type `oe graphs list --backend mac-mini` and press Enter.\n\nThe list appears again. Keep using this terminal, or set the token in each new terminal before sending work to the Mac mini.", async () => {
          await terminal.run("oe graphs list --backend mac-mini")
          await showRow("protect-with-a-token__terminal--access-corrected.png", terminal, /^NAME\s+VERSION/, "The Mac mini accepts the corrected token")
        })
        await step("Next, choose which computer your commands go to: see [Use OpenEngine on Another Computer](../test2doc-other-computers/test2doc-use-openengine-on-another-computer.mdx).", async () => {})
      } finally {
        if (running) await test.step("[nodoc] Stop the remote program", async () => {
          await remote.press("Control+C")
          await remote.waitForPrompt()
        })
        await remotePage.close()
      }
    })
  })
})

// Independent boundary checks keep both refusal cases observable even though
// the guide must stop at its first unmet requirement.
test.describe("Token access at the remote CLI boundary", { tag: ["@skip-docs"] }, () => {
  test("Accept the matching token without saving the secret in backend settings", async ({ engine, terminal }) => {
    await terminal.run(`export MAC_MINI_TOKEN='${TOKEN}'`)
    await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN`)
    await terminal.run("oe graphs list --backend mac-mini")
    const settings = readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8")
    expect(JSON.parse(settings).backends["mac-mini"].token_env).toBe("MAC_MINI_TOKEN")
    expect(settings).not.toContain(TOKEN)
  })

  for (const [name, value] of [["missing", ""], ["wrong", "wrong-example-token"]]) {
    test(`Refuse a ${name} token when accessing the other computer`, async ({ terminal }) => {
      await terminal.run(`export MAC_MINI_TOKEN='${value}'`)
      await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN`)
      // The exit status, rather than a particular error sentence, is the
      // observable refusal. The local engine's different token cannot help.
      await terminal.run("oe graphs list --backend mac-mini", { exitCode: 1 })
    })
  }
})
