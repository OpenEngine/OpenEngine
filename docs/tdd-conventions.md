# TDD conventions

Read by agent runs instead of working out how the repository is set up.
Sources: `AGENTS.md`, `README.md`, `pyproject.toml`, `cog.toml`,
`e2e/README.md`, `e2e/WRITING.md`, `apps/web/e2e/README.md`, the
`package.json` scripts and `.github/workflows/`.

## Setup

Requires uv, Python 3.11+ (CI tests 3.12–3.14) and Node.js 20.19+.

```bash
uv sync --all-packages              # CI uses: uv sync --locked --all-packages
npm --prefix apps/web install       # CI uses: npm --prefix apps/web ci
npm --prefix apps/web run build
```

- `uv.lock` must stay current: CI runs `uv lock --check`.
- Schema changes go in Alembic revisions, not runtime DDL:
  `migrations/sqlite` (state store), `migrations/sqlite_graph` (graph runtime,
  generate with `alembic_config(database_url, store="graph")`, apply with
  `engine-migrate --store graph <database_url>`). LangGraph owns its
  checkpoint schema.

## Tests

Python (tests in `tests/`, `testpaths = ["tests"]`):

```bash
uv run pytest                                   # unit/integration tier
uv run pytest --cov --cov-report=term-missing   # as CI runs it; fail_under = 84
uv run pytest -m compatibility                  # live providers; deselected by default
uv run ruff check
uv run --locked pyright
uv run pytest tests/test_boundaries.py tests/test_layout_helpers.py -v   # dependency direction
```

Web client (`apps/web`, unit tests run by vitest):

```bash
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run test
```

End to end:

- Browser: specs in `apps/web/e2e/`.
  `npx playwright install chromium` once, then
  `npm --prefix apps/web run test:e2e`.
- CLI: specs in `e2e/tests/`, run from `e2e/` with `E2E_AGENT=stub` (the default):
  `npm ci`, `npx playwright install --with-deps chromium`, `npm test`.
  `E2E_AGENT=real npm test` uses real Claude Code.

## Guides

- Source: CLI e2e specs in `e2e/tests/` whose describe blocks use
  `withDocCategory`/`withDocMeta`; categories in `e2e/tests/docCategories.ts`.
  Output is committed under `site/docs/guides/`.
- Writing rules: `e2e/WRITING.md` (reader is new and non-technical; one task
  per guide; start with why; one action per step; screenshot each command;
  say what to do if it fails; end with where to go next; use its glossary;
  never let the test show through). Setup/cleanup go in `[nodoc]` steps;
  harness-only specs are tagged `@skip-docs`.
- Generate (always with the stub agent): `cd e2e && npm run doc`, then commit
  the changes under `site/docs/guides/` with the spec.
- Restore: `git checkout -- site/docs/guides`.

## Docs site build

- Generator: Docusaurus in `site/` (`.github/workflows/pages.yml`).
- Triggered by changes to `site/**`, `v2/packages/contracts/**`, `e2e/**`,
  `scripts/install.sh`, `scripts/generate_cli_docs.py`,
  `cli/service/src/engine/graph_service/**`, `.github/workflows/pages.yml`.
- CI steps:
  1. `uv sync --locked --all-packages`
  2. in `e2e/`: `npm ci`, `npx playwright install --with-deps chromium`, `npm run doc`
  3. `cp scripts/install.sh site/static/install.sh`
  4. `uv run --project v2 --frozen python v2/packages/contracts/tooling/reference.py`,
     `uv run --project v2 --frozen python v2/packages/contracts/tooling/resources.py`,
     then `git diff --exit-code -- 'v2/packages/contracts/**/reference.html'`
  5. copy contract references into `site/static/contracts/` (gitignored)
  6. in `site/`: `npm ci`, `npm run build` (runs `scripts/generate_cli_docs.py`
     then `docusaurus build`; output `site/build/`, gitignored)
- `scripts/generate_cli_docs.py` rewrites the committed
  `cli/client/src/engine/cli/specs.py`, `site/docs/cli-specs.md` and
  `site/docs/cli-reference.md`; tests reject a stale reference.
- Restore: `git checkout -- site/docs/guides site/docs/cli-specs.md site/docs/cli-reference.md cli/client/src/engine/cli/specs.py`
  and remove the untracked `site/static/install.sh`.

## Security reproduction tests

The repository states no specific rule. Show a problem through a surface a
user reaches, using the existing tiers: the HTTP API (e.g.
`tests/test_web_app.py`), the browser e2e specs in `apps/web/e2e/` (real
server, scripted ACP agent from `tests/provider_fakes.py`, recording `gh`), or
the CLI e2e specs in `e2e/tests/` (real terminal, isolated daemon).

## Other

- Conventional Commits for commit messages and PR titles; allowed types:
  build, chore, ci, docs, feat, fix, perf, refactor, revert, style, test
  (`cog.toml`, enforced by `conventional-commits.yml`). No merge commits:
  squash or rebase.
- Git through the `git_subcommand` tool, not the shell.
- Open PRs with the `open_pull_request` tool, not `gh pr create`.
- Never push `engine/ws-*` branches; publish from a descriptive branch such as
  `agent/{description}` holding only the commits meant for the PR.
- `gh auth status`: run standalone; retry once with sandbox escalation before
  asking the user to reauthenticate. Never run `gh auth login`/`logout` or
  expose a token unless asked.
