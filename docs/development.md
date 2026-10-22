# Developing OpenEngine

This page is for people working on OpenEngine itself. If you only want to use
it, start with the [README](../README.md) or the
[getting started guide](https://openengine.cc/docs/getting-started/).

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 20.19+.

## Running from source

Clone the repository and install the workspace:
```bash
git clone https://github.com/OpenEngine/OpenEngine.git
cd OpenEngine
uv sync --all-packages  # install all workspace packages, editable
npm --prefix apps/web install
npm --prefix apps/web run build
```

Then run it by pointing it at your directory:
```bash
uv run \
  --project /path/to/openengine \
  --directory /path/to/your/project \
  --all-packages \
  engine-web
```

Or install the checked-out commit as your `engine`, in place of a release,
exactly as the one-line installer would. It is labeled with the commit, such as
`1.2.0-ffdb7cd`, and takes the installer's options:
```bash
scripts/install-local.sh
```

While working on OpenEngine itself, run the development server instead; it
watches the repository and restarts only what changed:
```bash
uv run engine-dev
```
See [dev-server.md](dev-server.md) for how it works.

The production service defaults to `http://127.0.0.1:4364`. Verify its identity
and readiness after startup:
```bash
curl --fail http://127.0.0.1:4364/api/health
# {"service":"openengine","version":"0.0.0","ready":true,"api_version":1}
```
`version` is the installed `engine-web` package version. Health returns HTTP 503
until startup completes, if the configured graph runtime cannot open, or during
shutdown; otherwise it returns HTTP 200. This public endpoint requires no browser
login and does not check external provider credentials.

## Terminal commands

The binary is `engine`. Run it with no arguments to list its commands; the full
reference is in [cli/README.md](../cli/README.md).

- `engine daemon` runs the local service in the background; see
  [Background service](releases.md#background-service).
- `engine connect gh|github|gitlab|slack` connects the selected backend's shared
  source control or Slack; `engine connections` shows them and
  `engine disconnect` undoes one.
- `engine agent add|get|remove|signin` and `engine agents` manage the agents
  graphs name: the built-in `claude`, `codex` and `opencode`, and any you add,
  such as `engine agent add opencode --name qwen --model qwen3-coder --url http://gpu.local:8000/v1`.
  Give an agent on a model server its context window with `--context` (and
  optionally `--output`) so opencode compacts its history before prompts
  outgrow it. For Ollama, set `PARAMETER num_ctx` in the Modelfile and
  `--context` to the same value: Ollama's OpenAI-compatible endpoint neither
  reports nor enforces `num_ctx`, so without the limit every step prefills the
  whole history.
- `engine agent claude`, `engine agent codex` and `engine agent opencode`
  start a run whose implementation node is that CLI in this terminal: the
  daemon checks the repository out into a fresh workspace and gives the agent
  the same `git_subcommand` and `open_pull_request` tools an implementation
  node gets (`[sessions] tools` in `engine.toml` changes which). The run ends
  when the CLI exits. Arguments after `--` go to the CLI.
- `engine graph`, `engine run`, `engine loop`, `engine node` and their list
  forms (`graphs`, `loops`, `nodes`) register, execute, schedule and steer
  graphs; `engine backend` chooses which daemon they talk to. See
  [cli/README.md](../cli/README.md).

While working on OpenEngine itself, run the development server instead:
```bash
uv run engine-dev
```


## engine.toml

The main configuration file for OpenEngine. The repository's own copy is
[`engine.toml`](../engine.toml) and documents every setting. The defaults are
sensible; to change them, point `engine-web` at your own file:
```bash
uv run \
  --project /path/to/openengine \
  --directory /path/to/your/project \
  --all-packages \
  engine-web \
  --config /path/to/engine.toml
```
Installed releases read `~/.config/openengine/engine.toml` instead.

## Database migrations

SQLite and PostgreSQL have independent Alembic histories. The PostgreSQL
history is currently a placeholder; the SQLite state store upgrades its
database on startup and can also be upgraded explicitly:
```bash
DATABASE_URL=sqlite:///conversations.sqlite3 uv run engine-migrate
```
You should not normally need to run this; it happens automatically on startup.

## Tests, linting and releases

- `uv run pytest` runs the Python suites; the end-to-end suite in [`e2e/`](../e2e)
  also generates the guides on the docsite.
- [ruff-baseline.md](ruff-baseline.md) and [pyright.md](pyright.md) describe the
  lint and type-check baselines CI enforces.
- Commits follow Conventional Commits; [releases.md](releases.md) explains how
  Release Please turns them into versions, changelog entries and release bundles.
- The docsite lives in [`site/`](../site) (Docusaurus). `npm --prefix site run build`
  checks links and builds it.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for how to get a change merged.
