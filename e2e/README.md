# e2e

End-to-end tests for the `oe` CLI, driven through a real terminal in a
browser. The same specs become the site's Guides.

```sh
npm ci                    # once
npm test                  # the suite, with scripted agents
npm run doc               # regenerate the guides in site/docs/guides/
E2E_AGENT=real npm test   # the suite again, with real Claude Code
```

## How it fits together

- `terminal/server.ts` runs each session in a real PTY (node-pty) and bridges
  it over a WebSocket. It listens on loopback only and needs the run's token.
- `terminal/page/` is the page Playwright opens: xterm.js with the DOM renderer,
  so every row is an element a test can find and a screenshot can annotate.
- `fixtures/terminal.ts` gives specs `terminal.run()`, `type()`, `press()`,
  the `waitFor…` helpers, and `row()`/`rows()` for highlighting output.
- `fixtures/engine.ts` starts an isolated engine daemon for each test. Its
  terminal sessions reach that daemon as `local`, so specs type plain
  `engine …`.

## Agents: stub by default, real on request

Graph runs use a **scripted agent** unless `E2E_AGENT=real` is set. The stub
is the production daemon with only the agent swapped for
`tests/provider_fakes.py`, scripted per test with `engine.script(...)`. It is
fast, it does exactly what a spec needs on cue, and CI uses it. Always
generate the guides with it too, so they come out the same every time.

`E2E_AGENT=real` runs Claude Code through its ACP adapter instead, to show
the whole chain works. It needs a Claude Code login and `npx`, and refuses to
start without them rather than falling back to the stub. Every step runs
`claude-haiku-5-5`, whichever tier it asks for; set `E2E_CLAUDE_MODEL` to a
full model ID to run a stronger one.

So that a spec passes in both modes:

- **Assert on state, never on wording.** Check statuses, what was registered
  and what exists, not what an agent said.
- **Keep example graphs tiny.** One or two steps, no tools unless the guide is
  about them, an explicit prompt ("reply with one sentence; don't run
  commands"), and no `model:` field, or only a tier: a step naming a model
  itself (`model: opus`) bypasses the cheap one real runs are given.
- **Script what needs choreography.** An agent asking an inline question, or
  a run still going when the test presses Ctrl+C, is something only the stub
  does on cue. Skip that part with
  `test.skip(engine.agentMode !== "stub", ...)`.

## Writing a guide

Guides are written for someone new to OpenEngine who isn't deeply
technical; `WRITING.md` says how, with a glossary every page uses.

A spec under `tests/` whose describe blocks use `withDocCategory` and
`withDocMeta` becomes a page. Every `test.step` title is a sentence the reader
follows ("Run `oe graphs list`."). Setup and cleanup go in `[nodoc]`
steps. Put each category in `tests/docCategories.ts`. Specs that test the
harness itself are tagged `@skip-docs`.

**The generated guides are committed.** After changing a guide spec, run
`npm run doc` and commit what changed under `site/docs/guides/` along with the
spec, so a reviewer reads the page itself in the pull request. The output is
byte-for-byte the same on every run, so only real changes show up in a diff.
The terminal page helps: it draws engine IDs (`g-…`, `run-…`), UUIDs,
timestamps and ports on this computer's loopback address (`127.0.0.1:…`,
`localhost:…`) as stand-ins that are the same on every run, and keeps the real
values in the buffer, where `terminal.text()` and every assertion read them.
Anything else that changes from run to run, such as a path on your machine,
keep off screen.

## The benchmark

`tests/benchmark/` holds the end-to-end benchmark (`docs/plans/e2e-benchmark.md`). It uses
real GitHub, real models and real tunnels, so it never runs in `npm test` or `npm run doc`:
everything in it is tagged `@benchmark`.

```sh
npm run oe-benchmark         # the journey; writes its guide to site/docs/benchmark/
npm run oe-benchmark:clean   # delete everything the last run left, once you have looked
```

It needs Docker. The journey happens on a clean Linux machine, a container built from
`benchmark/Dockerfile` (the first run builds it, then Docker keeps it): every terminal in the guide
is a shell inside it, so a screenshot shows nothing of your own setup, and nothing the run installs
or starts touches your computer. Your GitHub login is mounted into it read-only and copied in, so a
token the container refreshes never changes yours.

Sign in once with `npm run oe-benchmark:login`. It skips whatever is already signed in. For GitHub it checks
`gh` (in `GH_CONFIG_DIR`, if set) and, if needed, signs in with the token kept in a file rather than the system
keyring, which the container can't read. Claude and Codex are signed in inside Docker, by running
`claude auth login` and `codex login --device-auth` in the image. Each prints a link to open in a browser
and asks for the code it gives back. The logins are kept in two Docker volumes (`oe-bench-claude` and
`oe-bench-codex`) that every run mounts, so you sign in once, and they never touch your own Claude or Codex
login: on a Mac yours is in the keychain, which couldn't be copied anyway. A run stops at its start, not
minutes in, if either is missing. `npm run oe-benchmark:clean -- --logins` forgets them. This is for running on your own machine: the sign-in is interactive and a subscription's token expires, so a CI job would need
API keys for the runners instead, which the benchmark does not support yet.

It runs as the account `gh` is logged in as, and the terminal steps must be signed in as the same
account: if your `gh` picks its account by directory, set `GH_CONFIG_DIR` to the config directory to use
for the run, for example `GH_CONFIG_DIR=~/.config/gh-work npm run oe-benchmark`. The
benchmark stops before creating anything if the two GitHub accounts disagree. The token needs the `repo`, `workflow` and
`delete_repo` scopes (`gh auth refresh -h github.com -s workflow,delete_repo`), and the run checks them
before it creates anything. `workflow` is there because the issues add workflow files, and GitHub refuses a push
of one without it. A setup project runs first: it deletes the
`oe-bench` repository, its Pages site and the last run's container, then starts a new container. It
never touches a repository whose name doesn't start with `oe-bench` or that lacks the `oe-bench`
topic. After the run, pass or fail, a global teardown removes the container, and with it the tunnel
and daemon, but deletes no repository, so a failed run's repository is left to inspect.

At the end of a run it prints what the run noted about itself (the OpenEngine commit it tested, how long the
install took, how long GitHub took to list the webhook ping), and the same notes are in
`benchmark-results/results.json`, for whatever reads the results next. A test adds a note with
`testInfo.annotations.push({ type, description })`.

The last test in `tests/benchmark/benchmark.spec.ts`, "Verify the calculator", is the benchmark's only pass or fail for
the calculator: a fixed check of the deployed site (add, subtract, multiply, divide, divide by zero and clear), written
by a person and never shown to the agents. Each case is a step, none of them in the guide. It checks that the guide's run
worked, so `npm run oe-benchmark` runs it as its own step after the journey and only if the journey passed (the `verify`
project in `playwright-benchmark.config.ts`, without the guide's reporter, which would write a page for it, and without
setup, which would delete the repository it checks). It waits up to ten minutes for Pages to catch up with the last
merge, and `benchmark-results/verify.json` says which cases passed. The test's `@verify` tag is what keeps it out of the
journey's run and selects it for the `verify` project; running the config without `--project` would put it in the guide.
`support/verifier.ts` finds the keys by their button names and the display by its visible text, so nothing depends on
ids, classes or test ids.

`support/mask.ts` hides a secret in a guide screenshot. Pass `mask(locator)` among the targets
of `screenshot()`; it draws an opaque box over the element, which the benchmark must do for every
token or secret on screen.

## Real tunnels (opt in)

The anywhere guide uses local TLS by default and never looks for tunnel tools.
Run `E2E_TUNNEL=cloudflare npm test -- tests/reach-from-anywhere.spec.ts` for a
Cloudflare quick tunnel (install `cloudflared`; no account needed), or
`E2E_TUNNEL=tailscale npm test -- tests/reach-from-anywhere.spec.ts --workers=1`
on a dedicated machine signed in to Tailscale with HTTPS enabled. Existing
Tailscale Serve configuration is refused; the test removes only its own setup.
Missing tools or login fail the run, with no fallback. Agent selection remains
independent (`E2E_AGENT=stub` or `real`). The test checks the daemon refuses
unauthenticated API requests before exposing it, and records tunnel resources
for teardown. Generate committed guides with `E2E_TUNNEL` unset.
