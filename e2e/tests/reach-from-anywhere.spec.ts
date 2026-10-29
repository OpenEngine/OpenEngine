import { readFileSync } from "node:fs"
import type { Locator, TestInfo } from "@playwright/test"
import { withDocMeta } from "@test2doc/playwright/DocMeta"
import { screenshot } from "@test2doc/playwright/screenshots"
import type { Engine } from "../fixtures/engine.ts"
import type { Terminal } from "../fixtures/terminal.ts"
import { test, expect, GUIDE_ADDRESS, requireToken, type TunnelKind, type TunnelledComputer } from "../fixtures/tunnel.ts"
import { anotherComputerCategory } from "./docCategories"

// The Mac mini is reached at an https address, through local TLS unless E2E_TUNNEL asks for a real tunnel
// (see fixtures/tunnel.ts). Either way the page shows the address in GUIDE_ADDRESS, so the guide is the same.

// Tailscale Serve belongs to the machine, so the two sections take turns.
test.describe.configure({ mode: "default" })

test.use({ terminalSession: { cols: 120, rows: 30 } })

/** The Mac mini as this computer's backends file records it. */
function macMini(engine: Engine) {
  const file = JSON.parse(readFileSync(engine.env.ENGINE_BACKENDS_FILE!, "utf8"))
  return file.backends?.["mac-mini"] as { url: string; token_env?: string } | undefined
}

/** A step the page shows as a paragraph of its own (see another-computer.spec.ts). */
function step(text: string, body: () => Promise<void> = async () => {}) {
  return test.step(`\n${text}\n`, body)
}

/**
 * Screenshot the screen down to its last row in use, with `highlight` labelled. Nothing that
 * changes from run to run may show: not the port the OS picked for this computer's daemon, not a
 * real tunnel's address (the page draws the guide's in its place), and never the Mac mini's token.
 */
async function show(terminal: Terminal, testInfo: TestInfo, engine: Engine, macMiniComputer: TunnelledComputer, highlight: Locator, label: string) {
  const screen = terminal.screen()
  await expect(screen).not.toContainText(`:${new URL(engine.url).port}`)
  await expect(screen).not.toContainText(macMiniComputer.token)
  if (macMiniComputer.real) await expect(screen).not.toContainText(macMiniComputer.host)
  await screenshot(testInfo, highlight, {
    annotation: { text: label },
    clip: await terminal.usedArea({ labelBelow: true }),
  })
}

async function protectTheMacMini(kind: TunnelKind, computer: TunnelledComputer, engine: Engine) {
  const address = `https://${GUIDE_ADDRESS[kind]}`
  await step(
    `Before starting a tunnel, protect OpenEngine on the Mac mini with a **token**: a long, secret password that every request to it must carry. This matters. Keep OpenEngine private until this is set. A tunnel makes it reachable from outside your home network, and OpenEngine runs AI agents and commands on the Mac mini for whoever reaches it. With a token, anyone without it is turned away.

On the Mac mini, stop OpenEngine, make a token, and keep it in a file only you can read:

\`\`\`sh
oe daemon stop
mkdir -p ~/.config/openengine
(umask 077; touch ~/.config/openengine/.env)
chmod 600 ~/.config/openengine/.env
printf 'ENGINE_SERVICE_TOKEN=%s\\n' "$(openssl rand -hex 32)" >> ~/.config/openengine/.env
\`\`\`

Once this token is set, adding a repository with \`oe repo add\` or the web setup form is refused, even after signing in with the token. Adding a repository can grant agents permission to work on it, so it needs a separate onboarding token. Repositories already added keep working. The optional steps below show how to add more.

The commands finish without printing the secret. Type \`ls -l ~/.config/openengine/.env\`: the line must start with \`-rw-------\`, meaning only you can read it.`,
  )
  await step(
    `OpenEngine also only answers addresses it knows, so tell it the tunnel's. Open \`~/.config/openengine/engine.toml\` in a text editor and add this line at the top:

\`\`\`toml
public_url = "${address}"
\`\`\`

`,
  )
  await step(
    `Start OpenEngine again with \`oe daemon start\`, then type \`oe daemon status\`. Look for \`health: ready\`; if it is missing, check the configuration line and start again.

For commands on the Mac mini, load the token into each new terminal before using OpenEngine:

\`\`\`sh
set -a
. ~/.config/openengine/.env
set +a
\`\`\`

`,
  )
  await step(
    `To copy the token to this computer, type \`grep ENGINE_SERVICE_TOKEN ~/.config/openengine/.env\`. The output has this shape (your secret will be different):

\`\`\`text
ENGINE_SERVICE_TOKEN=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
\`\`\`

Copy everything after the \`=\`. Keep it secret, as you would a password.`,
    async () => {
      // The Mac mini as this step leaves it: a token of its own, and the tunnel's address as public_url.
      expect(computer.daemonEnv.ENGINE_SERVICE_TOKEN).toBe(computer.token)
      expect(computer.token).not.toBe(engine.env.ENGINE_SERVICE_TOKEN)
      expect(computer.publicUrl).toBe(`https://${computer.host}`)
      // Asked as the tunnel delivers it, and without the token, the Mac mini turns the request away.
      await requireToken(computer.url, computer.host)
    },
  )
}

/**
 * The OpenEngine half of either section: protect the Mac mini with a token, then add it from
 * this computer with that token and check it's up.
 */
async function addTheMacMini(
  kind: TunnelKind,
  { engine, terminal, computer, testInfo }: { engine: Engine; terminal: Terminal; computer: TunnelledComputer; testInfo: TestInfo },
) {
  const address = `https://${GUIDE_ADDRESS[kind]}`
  const typed = computer.publicUrl
  const clear = () => test.step("[nodoc] Clear the screen", async () => void (await terminal.run("clear")))

  await step(
    `The tunnel is now running. For browser access on either computer, open \`${address}\`, including when you are sitting at the Mac mini. Choose sign in and paste the token you copied earlier. Closing the browser does not sign you out; use Sign out when you finish. If GitHub login is configured instead, sign in with GitHub.

If the page does not open, check that OpenEngine and the tunnel are running on the Mac mini before continuing.`,
  )

  await step(
    `Back on this computer, tell OpenEngine about the Mac mini: its name, its address, and the name of the place its token will be kept, \`MAC_MINI_TOKEN\`. Type \`oe backend add mac-mini ${address} --token-env MAC_MINI_TOKEN\` and press Enter.

A **backend** is a computer where OpenEngine's background program is running, such as this one or the Mac mini. OpenEngine saves the Mac mini under the name \`mac-mini\`, with its address and \`MAC_MINI_TOKEN\` beside it. It never saves the token itself.`,
    async () => {
      await terminal.run(`oe backend add mac-mini ${typed} --token-env MAC_MINI_TOKEN`)
      expect(macMini(engine)).toMatchObject({ url: typed, token_env: "MAC_MINI_TOKEN" })
      await show(terminal, testInfo, engine, computer, await terminal.row(/^mac-mini\s+https:/), "The Mac mini is saved, with where to find its token")
    },
  )

  await clear()

  await step(
    `See the token at work before you give it to OpenEngine. Type \`oe backends list --check\` and press Enter.

Beside the Mac mini you'll see *down*, and that it *requires a token*: the Mac mini turned OpenEngine away, as it will anyone who doesn't have the token.`,
    async () => {
      const { output } = await terminal.run("oe backends list --check")
      expect(output).toMatch(/^\s+mac-mini\s.*\bdown\b/m)
      await show(terminal, testInfo, engine, computer, await terminal.row(/^\s+mac-mini\s.*\bdown\b/), "Without the token, the Mac mini turns you away")
    },
  )

  await clear()

  await step(
    `Now give this terminal the token. Type \`read -rs MAC_MINI_TOKEN && export MAC_MINI_TOKEN\` and press Enter, then paste the token and press Enter again.

Nothing shows while you paste, so the token never appears on your screen. The terminal keeps it until you close it; open a new terminal and you'll need to do this again.`,
    async () => {
      await terminal.type("read -rs MAC_MINI_TOKEN && export MAC_MINI_TOKEN")
      await terminal.press("Enter")
      await terminal.type(computer.token)
      await terminal.press("Enter")
      await terminal.waitForPrompt()
      await show(terminal, testInfo, engine, computer, await terminal.row(/read -rs MAC_MINI_TOKEN/), "The token is set, and never shown")
    },
  )

  await clear()

  await step(
    `Check that OpenEngine can reach the Mac mini now. Type \`oe backends list --check\` and press Enter.

You'll see *up* beside the Mac mini, followed by the agents it can use there. An agent is an AI coding assistant, such as Claude Code, that reads and changes code for you.

If you still see *requires a token*, the token you pasted isn't the Mac mini's: copy it again, and run the \`read\` command again. If you see *down* for another reason, check that the Mac mini is on, that OpenEngine and the tunnel are running on it, and that you typed its address correctly.`,
    async () => {
      const { output } = await terminal.run("oe backends list --check")
      expect(output).toMatch(/^\s+mac-mini\s.*\bup\b/m)
      await show(terminal, testInfo, engine, computer, await terminal.row(/^\s+mac-mini\s.*\bup\b/), "The Mac mini is up, from wherever you are")
    },
  )

  await step(
    `**Optional: add more repositories on the Mac mini.** The service token and browser sign-in cannot do this. Keep using the service token for everyday work; make a separate onboarding token only if you need to add repositories.

On the Mac mini, open a new terminal and save a new secret in the same private file:

\`\`\`sh
printf 'ENGINE_ONBOARDING_TOKEN=%s\\n' "$(openssl rand -hex 32)" >> ~/.config/openengine/.env
\`\`\`

It finishes without printing the secret. Keep this token private too: it allows repository setup, including decisions about whether agents may work without asking permission. If an onboarding token is already in the file, use that one instead of adding another.`,
  )
  await step(
    `On the Mac mini, load the saved tokens into this terminal:

\`\`\`sh
set -a
. ~/.config/openengine/.env
set +a
\`\`\`

The commands finish without printing the tokens. Repeat this in each new terminal where you add repositories.`,
  )
  await step(
    `Restart OpenEngine on the Mac mini so it can use the onboarding token:

\`\`\`sh
oe daemon stop
oe daemon start
oe daemon status
\`\`\`

Look for \`health: ready\` before continuing.`,
  )
  await step(
    `On the Mac mini, save a separate backend for repository setup:

\`\`\`sh
oe backend add onboarding http://127.0.0.1:4364 --token-env ENGINE_ONBOARDING_TOKEN
\`\`\`

The saved backend line shows \`onboarding\` and \`http://127.0.0.1:4364\`. This address reaches OpenEngine on the Mac mini itself; run this command there, not on your laptop.`,
  )
  await step(
    `Add your repository through that backend. Replace the example with your repository's address:

\`\`\`sh
oe repo add --backend onboarding https://github.com/your-name/your-repository
\`\`\`

Wait for \`Repository onboarded\`, or \`Already onboarded\` if it was added before. Use this command instead of the web setup form when you signed in with a service token. If it still refuses authentication, load the tokens in this terminal again and check that you restarted OpenEngine after saving the onboarding token.`,
  )

  await step(
    "Next, send your commands to the Mac mini: see [Use OpenEngine on Another Computer](./test2doc-use-openengine-on-another-computer.mdx), from the step where you switch to it.",
  )
}

test.describe(anotherComputerCategory, () => {
  test.describe(
    withDocMeta("Reach Your OpenEngine From Anywhere", {
      description: "Use OpenEngine on your Mac mini from anywhere, safely and over https, through Cloudflare Tunnel or Tailscale.",
      sidebar_position: 2,
      slug: "/guides/other-computers/reach-from-anywhere",
    }),
    () => {
      test.describe("Cloudflare Tunnel", () => {
        test.use({ tunnel: "cloudflare" })

        test("Reach it through Cloudflare Tunnel", async ({ engine, terminal, tunnelledComputer }, testInfo) => {
          const computer = tunnelledComputer!

          await step(
            `OpenEngine is a tool that has AI coding agents work on your code, step by step, in the way you lay out. If it runs on a Mac mini, or another computer that stays on all day, you can use it from your laptop wherever you are: at the office, at a café, or travelling. This guide shows how to do that safely, over https, with one of two free tools. Use whichever suits you.

**Cloudflare Tunnel** gives the Mac mini an address on a domain of your own, such as \`https://${GUIDE_ADDRESS.cloudflare}\`, that you can reach from anywhere. Nothing on your home network has to be opened up. You'll need a Cloudflare account with your domain added to it.`,
          )

          await step(
            "On the Mac mini, install `cloudflared`, Cloudflare's tunnel program. With [Homebrew](https://brew.sh), type `brew install cloudflared`; for other ways, see [Cloudflare's downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/). Run `cloudflared --version` afterwards: it should print `cloudflared version` followed by a version number.",
          )

          await step(
            "Sign in to Cloudflare. Type `cloudflared tunnel login` and press Enter. A browser window opens: sign in, and choose the domain the Mac mini's address will be on. Return to the terminal and look for `You have successfully logged in`. If it does not appear, complete the browser sign-in before continuing.",
          )

          await step(
            "Create a tunnel for OpenEngine. Type `cloudflared tunnel create openengine` and press Enter. A **tunnel** is a private way in from Cloudflare to the Mac mini, which the Mac mini opens itself. Look for `Created tunnel openengine with id` followed by a long identifier.",
          )

          await step(
            `Give the tunnel its address. Type \`cloudflared tunnel route dns openengine ${GUIDE_ADDRESS.cloudflare}\` and press Enter, with your own domain in place of \`example.com\`. Look for \`Added CNAME\` followed by your hostname.`,
          )

          await protectTheMacMini("cloudflare", computer, engine)

          await step(
            "Start the tunnel, sending what arrives to OpenEngine. Type `cloudflared tunnel run --url http://127.0.0.1:4364 openengine` and press Enter. Look for `Registered tunnel connection`, which means Cloudflare is connected. If it is missing, check your sign-in and tunnel name. Leave that terminal open: the tunnel works while it's running. To have it start by itself, see [Cloudflare's guide to running a tunnel as a service](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/as-a-service/).",
          )

          await addTheMacMini("cloudflare", { engine, terminal, computer, testInfo })
        })
      })

      test.describe("Tailscale", () => {
        test.use({ tunnel: "tailscale" })

        test("Reach it through Tailscale", async ({ engine, terminal, tunnelledComputer }, testInfo) => {
          const computer = tunnelledComputer!

          await step(
            `**Tailscale** joins your own computers into a private network of their own, called a **tailnet**, wherever each of them is. Only computers signed in to your tailnet can reach the Mac mini, and it gets an https address there, such as \`https://${GUIDE_ADDRESS.tailscale}\`. You don't need a domain.`,
          )

          await step(
            "Install Tailscale on both computers, the Mac mini and this one, from [Tailscale's downloads](https://tailscale.com/download).",
          )

          await step(
            "Sign in to Tailscale on both computers, with the same account, so they join the same tailnet. Then turn on https for your tailnet: see [Enabling HTTPS](https://tailscale.com/kb/1153/enabling-https). In Tailscale's admin console, open the Machines page, choose the Mac mini, and copy its full DNS name. Put `https://` in front of it: that is your address. Use it wherever this guide shows `https://mac-mini.tail1234.ts.net`.",
          )

          await protectTheMacMini("tailscale", computer, engine)

          await step(
            `On the Mac mini, give OpenEngine an https address on your tailnet. Type \`tailscale serve --bg 4364\` and press Enter. Look for \`Available within your tailnet\` and the address, such as \`https://${GUIDE_ADDRESS.tailscale}\`. Tailscale keeps serving it, even after a restart, until you type \`tailscale serve reset\`.`,
          )

          await addTheMacMini("tailscale", { engine, terminal, computer, testInfo })
        })
      })
    },
  )
})
