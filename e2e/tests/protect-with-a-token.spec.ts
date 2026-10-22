import { randomBytes } from "node:crypto"
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import type { Locator } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import { test, expect, type Engine } from "../fixtures/engine.ts"
import { anotherComputerCategory } from "./docCategories"
import { MAC_MINI, MAC_MINI_PATTERN } from "./macMini"

/** The token the reader set on the Mac mini: its own, not this computer's. */
const MAC_MINI_TOKEN = randomBytes(32).toString("hex")

/** A token the Mac mini doesn't know, such as an old one. */
const OLD_TOKEN = randomBytes(32).toString("hex")

/**
 * Testing gap: anotherComputerToken preconfigures the remote daemon; this test
 * does not execute the reader's .env edit or daemon restart on the Mac mini.
 * Manual check on the token-protected host: with ENGINE_SERVICE_TOKEN unset in
 * the shell and set in ~/.config/openengine/.env, `oe graph list` must succeed.
 */

/**
 * This computer's home folder, so the reader's ~/.config/openengine/.env and
 * the terminal's history of commands are this test's and nobody else's.
 */
const HOME = mkdtempSync(path.join(os.tmpdir(), "openengine-token-home-"))
const SECRETS = path.join(HOME, ".config", "openengine", ".env")
const HISTORY = path.join(HOME, ".bash_history")

test.afterAll(() => rmSync(HOME, { recursive: true, force: true }))

test.use({
  terminalSession: {
    cols: 120,
    rows: 30,
    env: { HOME, XDG_CONFIG_HOME: path.join(HOME, ".config"), HISTFILE: HISTORY },
  },
  anotherComputerAt: MAC_MINI,
  anotherComputerToken: MAC_MINI_TOKEN,
})

/** The Mac mini as the backends file records it. */
function macMini(engine: Engine) {
  const file = JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
  return file.backends?.["mac-mini"] as { url: string; token_env?: string } | undefined
}

/** What the reader saves in ~/.config/openengine/.env on this computer, in their text editor. */
function saveToken(token: string) {
  mkdirSync(path.dirname(SECRETS), { recursive: true })
  writeFileSync(SECRETS, `MAC_MINI_TOKEN=${token}\n`)
}

/** The Mac mini's row of `oe backends list --check`. */
function macMiniRow(output: string) {
  return output.split("\n").find((line) => /^\s+mac-mini\s/.test(line)) ?? ""
}

/**
 * A step the page shows, as a paragraph of its own: the generated Markdown
 * puts a step's text straight after whatever came before it.
 */
function step(text: string, body: () => Promise<void> = async () => {}) {
  return test.step(`\n${text}\n`, body)
}

test.describe(anotherComputerCategory, () => {
  test.describe(
    withDocMeta("Protect Your OpenEngine with a Token", {
      description: "Set a token on the OpenEngine on another computer, such as a Mac mini, so only you can use it, and give that token to the engine command on this one.",
      sidebar_position: 2,
      slug: "/guides/other-computers/protect-with-a-token",
    }),
    () => {
      test("Set a token on the Mac mini and use it from this computer", async ({ engine, anotherComputer, terminal, browser }, testInfo) => {
        /**
         * Screenshot the screen down to its last row in use, with `highlight`
         * labelled. Neither the port the OS picked for this computer nor a
         * token may show: the screenshots must come out the same on every run,
         * and a token is a secret.
         */
        const show = async (highlight: Locator, label: string) => {
          await expect(terminal.screen()).not.toContainText(`:${new URL(engine.url).port}`)
          await expect(terminal.screen()).not.toContainText(MAC_MINI_TOKEN)
          await expect(terminal.screen()).not.toContainText(OLD_TOKEN)
          await screenshot(testInfo, highlight, {
            annotation: { text: label },
            clip: await terminal.usedArea({ labelBelow: true }),
          })
        }
        const clear = () => test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))
        const check = async () => macMiniRow((await terminal.run("oe backends list --check")).output)

        await test.step("[nodoc] The Mac mini answers only to its own token", async () => {
          const ask = (token?: string) =>
            fetch(`${anotherComputer!.url}/api/v1/graphs`, token ? { headers: { Authorization: `Bearer ${token}` } } : {})
          expect((await ask()).status).toBe(401)
          expect((await ask(OLD_TOKEN)).status).toBe(401)
          expect((await ask(engine.env.ENGINE_SERVICE_TOKEN)).status).toBe(401)
          expect((await ask(MAC_MINI_TOKEN)).ok).toBe(true)
        })

        await test.step("[nodoc] A browser can sign in with the Mac mini token", async () => {
          const page = await browser.newPage()
          await page.goto(`${anotherComputer!.url}/api/health`)
          const request = (path: string, token?: string) => page.evaluate(async ({ path, token }) => {
            const response = await fetch(path, token === undefined ? {} : {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ token }),
            })
            return response.status
          }, { path, token })
          expect(await request("/api/v1/graphs")).toBe(401)
          expect(await request("/api/auth/token", OLD_TOKEN)).toBe(401)
          expect(await request("/api/auth/token", MAC_MINI_TOKEN)).toBe(200)
          expect(await request("/api/v1/graphs")).toBe(200)
          await page.context().clearCookies()
          expect(await request("/api/v1/graphs")).toBe(401)
          await page.close()
        })

        await step(
          `OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. When it runs on another computer, such as a Mac mini, anyone who can reach that computer's address could use it: start agents on your code, or change what it's set up to do. A **token** stops that. It's a long secret, like a password, that you set on the other computer. OpenEngine there then only answers commands that come with it.

This guide shows how to set a token on the Mac mini, give it to the \`oe\` command on this computer, and what you'll see if the token is missing or wrong. Each computer OpenEngine runs on is called a **backend**. You'll need OpenEngine already running on the Mac mini, and an address that reaches it from here: in this example, \`https://${MAC_MINI}\`. [Use OpenEngine on Another Computer](test2doc-use-openengine-on-another-computer.mdx) shows how to set that up.`,
        )

        await step(
          `On the Mac mini, make a token. In a terminal there, type \`openssl rand -hex 32\` and press Enter.

You'll see a long line of letters and numbers. That's your token. Keep it secret: anyone who has it can use OpenEngine on the Mac mini.`,
        )

        await step(
          `Still on the Mac mini, give the token to OpenEngine. Open the file \`~/.config/openengine/.env\` in a text editor, creating it if it isn't there, and add this line, with your token after the \`=\`:

\`\`\`text
ENGINE_SERVICE_TOKEN=your-token
\`\`\`

Save the file. So that only your account on the Mac mini can read it, type \`chmod 600 ~/.config/openengine/.env\` in its terminal and press Enter.

Then restart OpenEngine there so it reads the token: type \`oe daemon stop\`, press Enter, then type \`oe daemon start\` and press Enter. From now on, OpenEngine on the Mac mini answers only commands that come with this token. Commands you type on the Mac mini itself read the token from the same file. If you open its web page in a browser, enter the same token on the sign-in screen.`,
        )

        await step(
          `Back on this computer, tell OpenEngine about the Mac mini, and the name of the place where you'll keep its token. Type \`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN\` and press Enter.

OpenEngine saves the Mac mini under the name \`mac-mini\`, and remembers \`MAC_MINI_TOKEN\`: the name \`oe\` looks for the token under each time it talks to the Mac mini. The token itself is never saved in OpenEngine's list of computers.

If you already added the Mac mini without a token, type \`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN --replace\` instead: it updates the saved Mac mini to use \`MAC_MINI_TOKEN\`.`,
          async () => {
            await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN`)
            expect(macMini(engine)).toMatchObject({ token_env: "MAC_MINI_TOKEN" })
            await test.step("[nodoc] Add token authentication to an existing backend", async () => {
              await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --replace`)
              expect(macMini(engine)?.token_env).toBe("")
              await terminal.run(`oe backend add mac-mini https://${MAC_MINI} --token-env MAC_MINI_TOKEN --replace`)
              expect(macMini(engine)).toMatchObject({ token_env: "MAC_MINI_TOKEN" })
            })
            await show(await terminal.row(/^mac-mini\s+https:/), "The Mac mini is saved as mac-mini")
          },
        )

        await clear()

        await step(
          `Before giving this computer the token, see what happens without it. Type \`oe backends list --check\` and press Enter.

Beside the Mac mini you'll see *down*, and that it *requires a token*: OpenEngine reached it, but the Mac mini turned the command away because no token came with it. It names \`MAC_MINI_TOKEN\`, the place it looked for one. If you ever see this, give this computer the token, as in the next step.`,
          async () => {
            const row = await check()
            expect(row).toMatch(/\bdown: backend mac-mini requires a token; set MAC_MINI_TOKEN\b/)
            await show(await terminal.row(/^\s+mac-mini\s.*\bdown\b/), "No token: the Mac mini turns your commands away")
          },
        )

        await clear()

        await step(
          `Give this computer the token. On this computer, open the file \`~/.config/openengine/.env\` in a text editor, creating it if it isn't there, and add this line, with the token you made on the Mac mini after the \`=\`:

\`\`\`text
MAC_MINI_TOKEN=your-token
\`\`\`

Save the file. Keep the token in this file rather than typing it in the terminal: your terminal remembers the commands you type, so a token typed there could be read back later. Every \`oe\` command, in every terminal window, reads the token from this file.`,
          async () => saveToken(MAC_MINI_TOKEN),
        )

        await step(
          `So that only your account on this computer can read the file, type \`chmod 600 ~/.config/openengine/.env\` and press Enter.

Nothing appears; that's expected. Other people who use this computer, and programs they run, can no longer read your token.`,
          async () => {
            await terminal.run("chmod 600 ~/.config/openengine/.env")
            expect(statSync(SECRETS).mode & 0o777).toBe(0o600)
            await show(await terminal.row(/chmod 600/), "Only you can read the token file now")
          },
        )

        await clear()

        await step(
          `Type \`oe backends list --check\` and press Enter again.

This time you'll see *up* beside the Mac mini: it accepted the token, and your commands can use it.`,
          async () => {
            const row = await check()
            expect(row).toMatch(new RegExp(`^\\s+mac-mini\\s+https://${MAC_MINI_PATTERN}\\s.*\\bup\\b`))
            await show(await terminal.row(/^\s+mac-mini\s.*\bup\b/), "The Mac mini accepted your token")
          },
        )

        await clear()

        await step(
          `A token that doesn't match the one on the Mac mini is turned away too: say you made a new token there and this computer still has the old one. To see what that looks like, change the token in \`~/.config/openengine/.env\` on this computer to an old or mistyped one, save the file, and then type \`oe backends list --check\` and press Enter.

Beside the Mac mini you'll see *down*, and that it *refused the token in MAC_MINI_TOKEN*: a token came with the command, but not the one the Mac mini has.`,
          async () => {
            saveToken(OLD_TOKEN)
            const row = await check()
            expect(row).toMatch(/\bdown: backend mac-mini refused the token in MAC_MINI_TOKEN\b/)
            expect(row).not.toMatch(/requires a token/)
            await show(await terminal.row(/^\s+mac-mini\s.*\bdown\b/), "Wrong token: the Mac mini turns your commands away")
          },
        )

        await clear()

        await step(
          `To fix it, copy the token from the Mac mini again: it's the line starting \`ENGINE_SERVICE_TOKEN=\` in \`~/.config/openengine/.env\` there. On this computer, open \`~/.config/openengine/.env\`, paste it after \`MAC_MINI_TOKEN=\` in place of the old one, and save the file.

Then type \`oe backends list --check\` and press Enter. You'll see *up* beside the Mac mini again.`,
          async () => {
            saveToken(MAC_MINI_TOKEN)
            const row = await check()
            expect(row).toMatch(/\bup\b/)
            await show(await terminal.row(/^\s+mac-mini\s.*\bup\b/), "The right token: the Mac mini is up again")
          },
        )

        await test.step("[nodoc] No token is left in the terminal's history or readable by others", async () => {
          await terminal.run("history -w")
          const history = readFileSync(HISTORY, "utf8")
          expect(history).toContain("oe backends list --check")
          expect(history).not.toContain(MAC_MINI_TOKEN)
          expect(history).not.toContain(OLD_TOKEN)
          expect(statSync(SECRETS).mode & 0o077).toBe(0)
        })

        await step(
          "Next, switch your commands to the Mac mini and save a graph there: see [Use OpenEngine on Another Computer](test2doc-use-openengine-on-another-computer.mdx).",
        )
      })
    },
  )
})
