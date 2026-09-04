# OpenEngine

OpenEngine is your SDLC engine.
Changes -> Pool of Reviewers -> Reranking -> Impact Radius Analysis -> System Diagram -> Safe change 

## Getting started

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 20.19+.

OpenEngine uses your locally installed codex and claude CLI. This means that it can utilize your subscription limits instead of being provided an API key. Make sure your claude or codex CLI are installed and authenticated. 

First, clone the repo:
```bash
uv sync --all-packages  # install all workspace packages, editable
npm --prefix apps/web install
npm --prefix apps/web run build
```
Then, run it by pointing it at your project:
```bash
uv run \
  --project /path/to/your/project \
  --all-packages \
  engine-web
```

`engine-web` serves the client built into `apps/web/dist` and reads its
configuration once, so a source edit needs a rebuild, a Ctrl-C, or both. While
working on OpenEngine itself, run the development server instead:

```bash
uv run engine-dev
```

## Engine.toml
The main configuration file for OpenEngine. It's defined [here](./engine.toml).
While we use sensible defaults, if you need to configure engine, point it at a new 
`engine.toml` file.
```
uv run \
  --project /path/to/your/project \
  --all-packages \
  engine-web
  --config /path/to/engine.toml
```

SQLite and PostgreSQL have independent Alembic histories. The PostgreSQL
history is currently a placeholder; the SQLite state store upgrades its
database on startup and can also be upgraded explicitly:

```bash
DATABASE_URL=sqlite:///conversations.sqlite3 uv run engine-migrate ## you shouldn't have to run this, happens automatically on startup
```

## GitHub connection

OpenEngine connects to GitHub to open pull requests and post review comments.
The connection is set up once per machine through the Settings panel (gear icon
at the bottom of the sidebar).

### One-time setup: register an OAuth App

You need to create one GitHub OAuth App for your team. Each colleague then
pastes the client ID into their own Settings panel — no secrets are shared and
no server configuration is required beyond the step below.

1. Go to **github.com → Settings → Developer settings → OAuth Apps → New OAuth App**
2. Fill in the form (device flow does not use the callback URL, but GitHub
   requires one):
   - **Application name:** `OpenEngine`
   - **Homepage URL:** `http://localhost:8000`
   - **Authorization callback URL:** `http://localhost:8000`
3. Click **Register application**
4. On the app page, check **Enable Device Flow** and click **Update application**
5. Copy the **Client ID** (looks like `Ov23liXXXXXXXXXX`)

### Connecting

1. Open the Settings panel (gear icon in the sidebar)
2. Paste the Client ID into the field and click **Save**
3. Click **Connect GitHub**
4. The panel shows a short code and a link to **github.com/login/device**
5. Open that link, enter the code, click **Authorize**
6. The panel switches to **Connected** automatically

The OAuth token set is stored in the OS keychain (macOS Keychain, Secret
Service on Linux, Windows Credential Manager). When GitHub issues expiring
tokens, OpenEngine refreshes them automatically after an authorization failure
and retries the interrupted GitHub request once. Each colleague repeats steps
1–6 once with the same client ID.

### Environment variable fallback

If you deploy OpenEngine on a server where no keychain is available, set the
client ID and a pre-generated token as environment variables instead:

```toml
# engine.toml
github_client_id = "Ov23liXXXXXXXXXX"
github_token     = "ghp_XXXXXXXXXXXX"
```

Or via environment variables:

```bash
GITHUB_CLIENT_ID=Ov23liXXXXXXXXXX GITHUB_TOKEN=ghp_XXXXXXXXXXXX uv run engine-web
```

To diagnose interactive runner protocol incompatibilities, set
`ENGINE_AGENT_PROTOCOL_LOG` to a JSONL file before starting Engine. Codex and
Claude Code record normalized session and interaction events alongside their
runner-specific request shapes, parser outcomes, response actions, executable,
and hashed working-directory identity. The trace does not record prompts,
commands, approval wording, answers, schema property names, or property values.
The file is created with mode `0600` and rotates at 1 MB with three backups.

## What is it.

We are building OpenEngine, a system for automating the SDLC and SOP. The key differentiator of OpenEngine is that it is a system for configuring token flow rates and planning according to a timeline.

OpenEngine is fundamentally this: A planning agent which projects the timeline and relative issue + milestone sizes based on the user's stated goals. Then, it automates the distribution and production of the code required to reach those milestones according to the token flow rates set by the engine operator. 

The key concepts are:
- A "Project". An end-to-end product that the operator is working on. Timelines and milestones are associated with this.
- A "Workflow". Workflow runs belong to a Project. The orchestrator is able to kick off workflows which bake in the operators SDLC+SOP.
- A "Conversation". Workflows are comprised of these individual agent interactions. Some Conversations may be implementation. Some may be review. 

Fundamentally your project foreman schedules work, and dispatches work according to your budgets. You can use your subscription budgets, because OpenEngine uses claude and codex CLI under the hood. 

## Shape of the system

```
                   Workflow DSL
                  zero-dep Python
                       │
                       ▼
               ┌──────────────┐
Event + State →│    Engine    │→ Commands
               └──────────────┘
                       │
                       ▼
                    Runtime
                       │
             ┌─────────┼─────────┐
             ▼         ▼         ▼
          Temporal   Agents    Git/Buzz
```
