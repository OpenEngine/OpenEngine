# OpenEngine

OpenEngine is your SDLC engine.
Changes -> Pool of Reviewers -> Reranking -> Impact Radius Analysis -> System Diagram -> Safe change 

## Getting started

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 20.19+.
This is currently a source installation; the agreed path to Homebrew and a
verified shell installer is tracked in the
[portable distribution plan](docs/portability.md).

```bash
uv sync --all-packages  # install all workspace packages, editable
npm --prefix apps/web install
npm --prefix apps/web run build
uv run pytest      # run the suite, including the boundary checks
```

All three entrypoints run today and report their wiring:

```bash
uv run engine-web
```

SQLite and PostgreSQL have independent Alembic histories. The PostgreSQL
history is currently a placeholder; the SQLite state store upgrades its
database on startup and can also be upgraded explicitly:

```bash
DATABASE_URL=sqlite:///conversations.sqlite3 uv run engine-migrate
```

[`langgraph-acp/`](langgraph-acp/README.md) is a separate distribution, outside
this workspace on purpose: it depends on nothing OpenEngine ships and targets a
newer Python. `uv sync --all-packages` does not install it, and its suite is its
own -- CI runs it only when that directory changes.

```bash
cd langgraph-acp && uv run pytest
```

## Configuration

Each entrypoint accepts one provider-neutral TOML configuration file:

```bash
uv run engine-web --config ./engine.toml
uv run engine-worker --config ./engine.toml
uv run engine-control-server --config ./engine.toml
```

`--config` takes precedence over the `ENGINE_CONFIG` environment variable. If
neither is set, Engine reads `./engine.toml` when it exists, otherwise it uses
built-in defaults. Configurations are not merged.

Workflows start from the configured default branch unless their definition
explicitly names a different ref:

```toml
default_branch = "main"
```

For a repository whose primary branch is `master`, set
`default_branch = "master"`. If the configured branch is absent from `origin`,
Engine stops before running an agent and explains how to correct the setting.

Repository-owned workflows are selected with a directory relative to the
configuration file:

```toml
[workflows]
directory = "workflows"
```

Each non-private Python file in that directory imports `openengine` and exports
one immutable value named `workflow`. Definitions may form finite sequential or
branching graphs; cycles and parallel steps are intentionally unsupported in
the v1 DSL. Workflow files are trusted configuration and execute once during
startup. A compiled definition is snapshotted onto every run so editing or
removing a file does not change an in-flight run.

Engine also supports agent attribution, runner-specific settings, and
provider-neutral approval policy:

```toml
attribution = false

[claude]
output_style = "concise"

[approvals]
auto_approve = false
allow = ["read"]

[approvals.bash]
allow = [
  "uv run pytest **",
  "git status **",
]
ask = ["git push **"]
deny = ["sudo **"]
```

Set `attribution = false` to keep both Codex and Claude Code from adding agent
attribution to commits and pull requests. Attribution is enabled by default.

`[claude]` holds settings only the Claude Code runner can honour, which is why
they are a table of their own rather than top-level keys. Set
`claude.output_style` to `concise`, `explanatory`, or `learning` to fix how
Claude writes its answers: every Claude Code runner this composes — chat,
read-only, and workflow — selects the matching Claude output style before the
turn starts. Omitting the key leaves Claude's own default in place.

Capabilities are `read`, `edit`, `bash`, `web`, and `mcp`. Configuration is
strict: unknown keys, unknown capabilities, duplicate entries, and incorrectly
typed values stop startup with an error instead of silently weakening a policy.

The policy is enforced in two places, and each runner's adapter owns the
translation between Engine's vocabulary and its provider's:

* **Before the turn**, `approvals.allow` builds the interactive Claude Code
  runner's `--allowedTools`: a preapproved tool never raises a request at all.
  Codex has no equivalent, because its pre-turn knob is a sandbox — a ceiling
  rather than a preapproval, and one narrowed from the policy would refuse a
  write that a person had just approved.
* **During the turn**, every approval request a provider raises is classified
  back into a capability and answered from the same policy. Allowed and denied
  requests are recorded with a `policy` decision source and nobody is shown
  them; anything the policy has not ruled on is put to a person, including any
  request no runner could classify.

Shell is deliberately never preapproved to the provider, because a shell policy
is written per command rather than per capability: `Bash` reaches the approval
callback on both runners, and `approvals.bash` is applied there. Patterns are
globs over the command line, where a trailing `**` also matches no arguments at
all, and the most restrictive matching rule wins -- `deny`, then `ask`, then
`allow`. An explicit `deny` also outranks `auto_approve`.

Workflow implementation and review runners are not built from the policy: what
they may do is a property of the step, and a reviewer that cannot write should
not become one that can because a chat was granted `edit`. The approvals they
raise mid-run are governed like any other.

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

The token is stored in the OS keychain (macOS Keychain, Secret Service on
Linux, Windows Credential Manager). Each colleague repeats steps 1–6 once with
the same client ID.

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
