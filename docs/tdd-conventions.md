# TDD conventions for agent runs

Collected from AGENTS.md, CONTRIBUTING.md, docs/development.md, e2e/README.md,
e2e/WRITING.md, pyproject.toml, the package.json scripts and
`.github/workflows/`. None of these commands were run when this file was written.

## Setup

Requires uv, Python 3.11+ and Node.js 20.19+ (the `e2e/` package wants Node
22.18+).

```bash
uv sync --all-packages            # CI uses: uv sync --locked --all-packages
npm --prefix apps/web install     # CI uses: npm --prefix apps/web ci
npm --prefix apps/web run build   # the Python server serves apps/web's built client
```

For the CLI end-to-end suite and guides, also run:

```bash
cd e2e && npm ci && npx playwright install --with-deps chromium
```

For the browser end-to-end suite, also run:
`npm --prefix apps/web exec -- playwright install --with-deps chromium`.

## Tests

- Python unit/integration tests live in `tests/` (`testpaths = ["tests"]`).
  Run them with `uv run pytest`. CI runs `uv run pytest --cov --cov-report=term-missing`
  and fails below 84% coverage. Tests marked `compatibility` (live providers)
  are deselected by default.
- Architecture boundaries: `uv run pytest tests/test_boundaries.py tests/test_layout_helpers.py -v`.
- Lint and types: `uv run ruff check`, `uv run --locked pyright`, `uv lock --check`
  (see docs/ruff-baseline.md and docs/pyright.md).
- Web client (`apps/web`): `npm --prefix apps/web run lint`, `run typecheck`,
  `run test` (vitest), `run build`.
- Browser end to end: specs in `apps/web/e2e/`. Run `npm --prefix apps/web run test:e2e`.
- CLI end to end: specs in `e2e/tests/`. Run `cd e2e && E2E_AGENT=stub npm test`
  (CI sets `E2E_AGENT=stub`; `E2E_AGENT=real` runs real Claude Code).
- v2: run `uv sync` and `uv run pytest` inside `v2/` (v2/README.md).
  Cross-NATS tests are in `v2/integration_tests/`.

## Guides

- User guides are in `site/docs/guides/test2doc-*`. They are generated from the
  CLI e2e specs in `e2e/tests/` that use `withDocCategory` and `withDocMeta`,
  so edit the spec and not the page. Categories go in `e2e/tests/docCategories.ts`.
  Tag harness-only specs `@skip-docs`.
- Writing rules are in `e2e/WRITING.md`. Write for a reader new to OpenEngine.
  Each guide covers one task and opens with why. Each step is one action with
  a screenshot of success. Use the glossary's wording. Never mention
  verify/assert/expect/fixture/stub/mock. Reader-invisible setup goes in
  `[nodoc]` steps. Assert on state, never on wording.
- Generate them with `cd e2e && npm run doc`, always with the stub agent.
  The output is deterministic, and it is committed together with the spec.
- Restore them with `git checkout -- site/docs/guides`.

## Docs site build

- Docusaurus is in `site/`, built by `.github/workflows/pages.yml`.
- Changes to these paths trigger it on pull requests and on pushes to main:
  `site/**`, `v2/packages/contracts/**`, `e2e/**`, `scripts/install.sh`,
  `scripts/generate_cli_docs.py`, `cli/service/src/engine/graph_service/**`,
  `.github/workflows/pages.yml`.
- CI steps:
  1. `cd e2e && npm ci && npx playwright install --with-deps chromium && npm run doc`
  2. `cp scripts/install.sh site/static/install.sh`
  3. `uv run --project v2 --frozen python -m openengine.contracts.reference`
     and `uv run --project v2 --frozen python v2/packages/contracts/tooling/resources.py`,
     then `git diff --exit-code -- 'v2/packages/contracts/**/reference.html'`
     (the committed references must be current).
  4. Copy the contract references and schemas into `site/static/contracts/`.
  5. `cd site && npm ci && npm run build`. This runs `scripts/generate_cli_docs.py`
     and then `docusaurus build`, with output in `site/build`.
- Restore with `git checkout -- site/docs v2/packages/contracts` (the tracked
  generated files, including `site/docs/cli-reference.md`) and
  `rm -rf site/build site/static/install.sh site/static/contracts`
  (untracked or ignored output).

## Security reproduction tests

From `docs/examples/graphs/security-review.yaml` and SECURITY.md:

- Show the problem through the real public entry point a user or attacker can
  reach, such as a CLI, daemon HTTP API route, web UI, webhook, MCP/tool
  handler or Slack. Use stub dependencies and synthetic data.
- Do not test by calling an internal helper that bypasses the boundary, and do
  not reimplement the vulnerable logic.
- Record the setup, the exact command or request, the expected result and the
  actual result.
- Never probe live deployments, third-party services, real accounts, host
  credentials or secrets.
- Existing targeted tests in `tests/` (HTTP surface, fake ACP agent) are the
  model. A regression test asserts the boundary holds.

## Other

- Commits and PR titles use Conventional Commits. Allowed types: build, chore,
  ci, docs, feat, fix, perf, refactor, revert, style, test, with an optional
  scope such as `feat(cli):`. CI checks PR titles and every commit (cocogitto, `cog.toml`).
- Run git through the `git_subcommand` tool, not the shell. Open PRs with
  `open_pull_request`, not `gh pr create`.
- Never push `engine/ws-*` branches. Create a descriptive branch such as
  `agent/{description}` from the intended base, containing only the PR's commits.
- Keep PRs focused. Branch from `main`. Describe what changed, why, how it was
  tested, and link the issue.
- Make schema changes in Alembic revisions (`migrations/sqlite`, and
  `migrations/sqlite_graph` for the graph store), never in runtime DDL.
- GitHub auth: run `gh auth status` standalone, retry once escalated, and never
  run `gh auth login`/`logout` or expose tokens.
