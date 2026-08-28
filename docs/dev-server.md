# Development server

Status: proposed

This plan replaces the manual edit-build-restart loop for a source checkout
with one command that watches the repository, applies UI changes to the open
page, and restarts the Python server when — and only when — a change requires
it.

## The loop today

A checkout has three tiers that react to an edit differently, and nothing tells
you which one you just touched:

- Editing `apps/web/src/*.tsx` changes nothing a browser can see until
  `npm --prefix apps/web run build` runs. The Python process serves Vite's
  output from `apps/web/dist` (`STATIC_DIRECTORY` in
  `apps/web/src/engine/apps/web/__main__.py`), so an unbuilt edit is invisible
  rather than broken, which is the failure mode that wastes the most time.
- Editing any Python under `packages/` or `apps/web/src/engine` requires
  Ctrl-C and `uv run engine-web` again. `uvicorn.run(app, ...)` is handed a
  constructed application object, so there is no reloader in the picture.
- Editing `engine.toml` or a file under `workflows/` also requires a restart,
  for a less obvious reason: `main()` calls `load_engine_config` and
  `load_workflow_catalog` once, and a compiled workflow definition is
  snapshotted onto every run. A workflow edit that appears to do nothing is
  the same class of confusion as an unbuilt `.tsx`.

`apps/web/vite.config.ts` already configures a dev server on port 5173 that
proxies `/api` to `http://localhost:8000`, so half of this exists and is
undocumented. The plan is mostly about making both halves start together and
making the Python half reloadable.

## Shape: one supervisor, two watchers that already ship

We are not writing a file watcher. Two mature ones are already dependencies,
and each is better at its tier than a hand-rolled one would be:

- **Vite** owns the UI. With its dev server in front of the browser, "rebuild
  the UI" largely stops existing: a `.tsx` edit is hot-swapped into the running
  page in tens of milliseconds with component state preserved, instead of a
  full `vite build` and a hard refresh. This is strictly better than the
  build-on-change loop the title asks for, so we should take it rather than
  rebuild `dist/` on every keystroke.
- **Uvicorn's `--reload`** (watchfiles) owns the Python tier. It restarts the
  application process on source changes and already handles the debounce,
  the child-process lifecycle, and the "changed file X" reporting.

`engine-dev` is then a supervisor, not a build system: it starts both, picks
ports, tells each about the other, merges their logs behind `[api]` and `[web]`
prefixes, and tears both down together so a crashed Vite never leaves a stray
uvicorn holding port 8000.

## What has to change for `--reload` to work

Uvicorn's reloader re-imports the application in a fresh child process on every
change, so it needs an import string rather than an object. Three small changes
follow from that, and no wiring is duplicated:

1. **Extract an application factory.** The body of `main()` that builds
   capabilities, runners, the session, and `create_app` moves into
   `build_app()` in `engine.apps.web.__main__`. `main()` calls it; the reloader
   names it as `engine.apps.web.__main__:build_app` with `factory=True`. One
   composition path, two callers.
2. **Pass configuration through the environment.** The reload child parses no
   argv of ours, so `--config` cannot reach it directly. `ENGINE_CONFIG`
   already exists for exactly this and already ranks below `--config` in
   precedence, so the supervisor sets it for the child and nothing new is
   invented.
3. **Make the Vite proxy target configurable.** `vite.config.ts` hardcodes
   `http://localhost:8000`. It should read `ENGINE_API_URL` with that value as
   the default, so the supervisor can fall back to a free port when 8000 is
   taken. `apps/web/e2e/harness.ts` already picks free ports this way, so the
   technique has precedent in the repo.

### What is watched, and what must not be

The reloader watches `packages/`, `apps/web/src/engine`, `workflows/`, and
`engine.toml` — the last two because they are startup-time reads, which is the
whole of "restart if necessary".

Exclusions matter more than inclusions here, because two of them would
otherwise produce a server that restarts continuously while doing normal work:

- `conversations.sqlite3` and its `-wal`/`-shm` siblings are written into the
  working directory on every message. Watching the repository root without
  excluding them means every chat turn restarts the server mid-turn.
- `apps/web/dist` and `apps/web/node_modules` are build output and vendor code.
- Agent worktrees live under `workspace_root`, which defaults to
  `/tmp/engine-workspaces` and is therefore already outside the tree. This is
  worth an explicit note in the code: pointing `workspace_root` inside the
  repository would make every agent edit restart the server underneath the
  agent.

## Restarts are not free, and the plan says so

The web process owns in-flight agent runs. `ThreadService._active_runs` holds
an `ActiveRun` per agent instance, each driving a provider CLI subprocess and
an open `text/event-stream` or `application/x-ndjson` response to the browser.
A reload kills all of that: the CLI subprocess dies, the stream ends, and the
run keeps only what was already persisted.

Phase 1 accepts this and documents it, because stock `--reload` is small and
the common case — editing a route handler while nothing is running — is
unaffected. If it turns out to bite in practice, phase 2 moves the Python watch
into the supervisor (watchfiles directly), which can then ask the server
whether a run is live and hold the restart:

```
[api] 2 files changed; restart deferred, 1 run in flight (press r to restart now)
```

Deferral is deliberately not in the first phase. It replaces working machinery
with our own, and it should be paid for by a real complaint rather than an
anticipated one.

## Dependency and schema changes

Editing `pyproject.toml`, `uv.lock`, `package.json`, or `package-lock.json`
means the running processes are stale in a way a restart does not fix. The
supervisor detects those paths and prints the one command that fixes it —
`uv sync --all-packages` or `npm --prefix apps/web install` — rather than
running it. Both are slow enough, and mutate the environment enough, that a
tool doing it unannounced during someone's edit is worse than a printed line.
An `--auto-sync` flag can opt in later.

Alembic needs nothing: the SQLite state store upgrades its database on startup,
so a new revision is applied by the restart that its own source change already
triggers.

## Command surface

```bash
uv run engine-dev              # both tiers; open http://localhost:5173
uv run engine-dev --no-web     # API only, reloading; for backend-only work
uv run engine-dev --build      # serve built dist from :8000, production shape
```

`--build` exists so the served-from-Python path — SPA fallback routes, the
`BuiltClient` cache headers, asset hashing — stays exercisable by hand, since
the Vite dev server bypasses all of it and that is where a "works in dev only"
bug would hide.

## Where the code goes

`engine-dev` is a console script on `apps/web`, implemented in a new
`apps/web/src/engine/apps/web/dev.py`. It is deliberately not a new workspace
package: `tests/test_packages.py` pins the package roots in
`EXPECTED_PACKAGE_ROOTS`, and a developer convenience is not a capability. The
module imports the standard library and its own entrypoint only, so
`tests/test_boundaries.py` is unaffected.

## Acceptance criteria

1. `uv run engine-dev` serves the interface and the API, and Ctrl-C leaves no
   surviving child process.
2. Editing a `.tsx` updates the open page without a manual build or refresh.
3. Editing a Python file under `packages/` restarts the API and the page keeps
   working after the restart.
4. Editing `engine.toml` or a file under `workflows/` restarts the API, and the
   change is visible in the restarted process.
5. A chat turn that writes to `conversations.sqlite3` does not cause a restart.
6. Streaming survives the Vite proxy: an agent turn's `text/event-stream` and
   `application/x-ndjson` responses arrive incrementally through port 5173, not
   buffered to completion. This is the highest-risk item in the plan and is
   checked first.
7. `uv run engine-web` behaves exactly as it does today.

## Tests

Proportionate to a developer tool — the supervisor's decisions are tested, its
subprocesses are not:

- `tests/test_dev_server.py`: the reload configuration is right. Watched
  directories include `workflows/` and `engine.toml`; exclusions cover
  `*.sqlite3*`, `dist/`, and `node_modules/`; `ENGINE_CONFIG` and
  `ENGINE_API_URL` are set for the children. No process is spawned.
- One test in `tests/test_web_app.py` that `build_app` returns a working
  application, which is the contract `--reload` depends on and the one thing
  here that can silently break the production entrypoint.
- A vitest over the extracted proxy-target helper in `apps/web`, so the
  `ENGINE_API_URL` default is pinned.

## Non-goals

- Not a production process manager. `engine-web` remains the way to run the
  interface, and the portable distribution
  ([portability plan](portability.md)) ships built assets rather than a
  watcher.
- Not `engine-worker` or `engine-control-server`. The same factory-plus-reload
  pattern extends to them once the web loop has proven it; doing all three now
  triples the change for one tier's worth of evidence.
- Not a replacement for the e2e harness's unconditional `npm run build`. That
  build exists to keep the browser tier deterministic, which is the opposite of
  what a dev server optimizes for.

## Phases

1. `build_app` factory, `ENGINE_API_URL` in the Vite config, the `engine-dev`
   supervisor, tests, and a README section. This is the whole user-visible
   feature.
2. Restart deferral while runs are in flight — only if phase 1's caveat is
   actually felt.
3. The same treatment for `engine-worker` and `engine-control-server`.
