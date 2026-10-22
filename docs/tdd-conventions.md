# Conventions for agent runs

Collected from README.md, AGENTS.md, `e2e/README.md`, `e2e/WRITING.md`,
`apps/web/e2e/README.md`, `pyproject.toml`, `cog.toml`, the package scripts and
`.github/workflows/`. Commands run from the repository root unless noted.

## Setup

Requires uv (CI pins 0.9.28), Python 3.11+ per README (CI tests 3.12, 3.13 and 3.14;
`.python-version` is 3.14) and Node.js 20.19+ (CI uses 22 for `apps/web`, 24
for `e2e/` and `site/`).

```sh
uv sync --all-packages              # CI: uv sync --locked --all-packages
npm --prefix apps/web install       # CI: npm --prefix apps/web ci
npm --prefix apps/web run build
```

- `uv.lock` must be current: `uv lock --check`.
- Browser end-to-end tests: `npm --prefix apps/web exec -- playwright install --with-deps chromium`.
- CLI end-to-end tests and guides: `cd e2e && npm ci && npx playwright install --with-deps chromium`.

## Tests

Python (in `tests/`, `testpaths = ["tests"]`):

```sh
uv run pytest                                   # excludes -m compatibility by default
uv run pytest --cov --cov-report=term-missing   # CI; coverage fail_under = 84
uv run ruff check
uv run --locked pyright
uv run pytest tests/test_boundaries.py tests/test_layout_helpers.py -v   # dependency direction
```

Tests marked `compatibility` drive real provider agents; they are deselected
here and run on a schedule (`acp-compatibility.yml`).

Web client (`apps/web`, vitest unit tests):

```sh
npm --prefix apps/web run lint
npm --prefix apps/web run typecheck
npm --prefix apps/web run test
npm --prefix apps/web run build
```

End to end:

- Browser (`apps/web/e2e/`, Playwright against a real server and a scripted ACP
  agent): `npm --prefix apps/web run test:e2e`
- CLI (`e2e/tests/`, Playwright driving a real terminal): `cd e2e && E2E_AGENT=stub npm test`.
  `E2E_AGENT=real` uses real Claude Code instead; CI uses the stub.

## Guides

- Source: the CLI end-to-end specs in `e2e/tests/`. A spec whose describe
  blocks use `withDocCategory` and `withDocMeta` becomes a page; categories go
  in `e2e/tests/docCategories.ts`. Specs that test the harness, or must not be
  published, are tagged `@skip-docs`. Setup and cleanup go in `[nodoc]` steps.
- Writing rules: `e2e/WRITING.md` (reader new to OpenEngine, one task per
  guide, start with why, one action per step, a screenshot for every command,
  everyday words, the glossary, no test vocabulary). Specs assert on state,
  never on wording (`e2e/README.md`).
- Generate: `cd e2e && npm run doc` (with the stub agent). Output goes to
  `site/docs/guides/`.
- The generated guides are committed: commit what changed under
  `site/docs/guides/` together with the spec. The output is byte-for-byte
  stable across runs.
- Restore: the guides are tracked in git, so `git restore site/docs/guides`
  returns tracked files to the committed version; remove any new untracked
  files the generator left.

## Docs site build

Docusaurus in `site/`, built by `.github/workflows/pages.yml`.

- Triggered on pull requests and pushes to main touching: `site/**`,
  `v2/packages/contracts/**`, `e2e/**`, `scripts/install.sh`,
  `scripts/generate_cli_docs.py`, `cli/service/src/engine/graph_service/**`,
  `.github/workflows/pages.yml`.
- Steps CI runs:
  1. `uv sync --locked --all-packages`
  2. `cd e2e && npm ci && npx playwright install --with-deps chromium && npm run doc`
  3. `cp scripts/install.sh site/static/install.sh`
  4. `uv run --project v2 --frozen python v2/packages/contracts/tooling/reference.py`
     and `.../tooling/resources.py`, then
     `git diff --exit-code -- 'v2/packages/contracts/**/reference.html'`
     (the committed references must be current)
  5. copy contract references and schemas into `site/static/contracts/`
  6. `cd site && npm ci && npm run build` (runs `python3 ../scripts/generate_cli_docs.py`
     then `docusaurus build`)
- `scripts/generate_cli_docs.py` rewrites committed files:
  `cli/client/src/engine/cli/specs.py`, `site/docs/cli-specs.md` and, when run
  as `uv run python scripts/generate_cli_docs.py`, `site/docs/cli-reference.md`.
  Tests reject a stale reference.
- Output: `site/build/` (gitignored, as is `build/`). Restore the committed
  files the build touched with `git restore` on the paths above, and delete
  untracked copies (`site/static/install.sh`, `site/static/contracts/`) if the
  run created them.

## Security reproduction tests

The repo names no dedicated kind. Tests that reach the code the way a user or
client does:

- Python tests in `tests/` against the HTTP surface (Starlette `TestClient` or
  request objects, e.g. `tests/test_request_security.py`,
  `tests/test_github_login.py`), run with `uv run pytest <file>`.
- Browser end-to-end specs in `apps/web/e2e/` (real server, scripted agent).
- CLI end-to-end specs in `e2e/tests/`; tag them `@skip-docs` so they are not
  published as guides.

## Other

- Conventional Commits for commit messages and pull request titles. Allowed
  types (`cog.toml`, `conventional-commits.yml`): build, chore, ci, docs, feat,
  fix, perf, refactor, revert, style, test. CI checks the PR title and every
  commit (`cog check`).
- No merge commits; history stays linear (squash or rebase).
- Git goes through the `git_subcommand` tool, not the shell; open pull
  requests with `open_pull_request`, not `gh pr create`.
- Never push `engine/ws-*` branches. Publish from a descriptive branch such as
  `agent/{description}` created from the intended base, holding only the
  commits meant for the pull request.
- `gh auth status`: run it standalone; on failure retry once with sandbox
  escalation; never run `gh auth login`/`logout` or expose a token.
- Schema changes go in Alembic revisions (`migrations/sqlite` for the state
  store, `migrations/sqlite_graph` for the graph runtime database), not runtime
  DDL. LangGraph owns its checkpoint schema.
