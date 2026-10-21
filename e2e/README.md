# e2e

End-to-end tests for the `engine` CLI, driven through a real terminal in a
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
follows ("Run `engine graphs list`."). Setup and cleanup go in `[nodoc]`
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
