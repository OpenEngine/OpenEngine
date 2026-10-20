# Onboarding a repository

Run `engine repo add .` or `engine repo add /path/to/repo` to register an
existing GitHub or GitLab checkout. Run `engine repo add <github-or-gitlab-url>`
to clone and register a remote repository, preserving its HTTPS or SSH transport.
Use `--path /absolute/checkout` to select the clone destination. Connected mode
is the default; use `--mode disconnected` for local-only work.

By default a pathless onboarding clones into `~/code/repo`. Set
`[onboarding] directory` in `engine.toml` to change that parent directory, for
example `directory = "~/code/work"`, so a deployment that keeps checkouts
elsewhere need not pass `--path` on every call, from every surface. An explicit
`--path` (CLI, web, MCP) and an existing checkout of the same remote still take
precedence. When set, a relative value resolves against the configuration
file's directory, as do the other configuration-relative paths; an empty value
keeps the `~/code` default.

The web UI offers **Add repository** on the WorkOrder form and on first run.
MCP clients can call `onboard_repository(repository, path?, mode?)`, then poll
`repository_onboarding_status(job_id)` using the returned `id`. All three surfaces
use the same daemon job and display its progress and actionable failures.

The daemon account needs git and a signed-in agent (`engine agent signin claude`, `codex`, or `opencode`).
Cloning uses the daemon account’s existing credentials for the supplied URL.
Existing checkouts do not require remote access. Preflight failures leave the checkout and configuration untouched. Onboarding
never changes credentials. Existing checkouts of the same remote are reused.
Repeating successful onboarding reports that the repository is already onboarded.

Onboarding writes to `$XDG_CONFIG_HOME/openengine/engine.toml`, defaulting to
`~/.config/openengine/engine.toml`, unless the daemon selected an explicit
`--config` / `ENGINE_CONFIG` file. It preserves existing settings and comments
in that destination and leaves automatically discovered project-local files
untouched. It updates the running daemon immediately and
registers the four starter graphs for the `owner/repo` project. No restart is needed.
The repository is ready to select in the WorkOrder form; the CLI prints a
`engine graph run review --project owner/repo --repo /checkout/path --input ref=HEAD` command.

The web daemon accepts loopback hostnames (`localhost`, `127.0.0.1`, and `::1`).
For access through another hostname or a reverse proxy, set `public_url` in the
loaded configuration to the external URL, including its port when nonstandard.
Requests to other hostnames are rejected, including read-only requests.

## Manual configuration reference

Use the untracked `.engine/config.toml` in the server's working directory to
configure repositories and approvals without editing the committed `engine.toml`.
Configuration selection is: explicit `--config`, then `ENGINE_CONFIG`, then the machine config
(`$XDG_CONFIG_HOME/openengine/engine.toml`, defaulting to
`~/.config/openengine/engine.toml`), then `.engine/config.toml`, then the root
`engine.toml`. Use `--config .engine/config.toml` to explicitly select local
settings when a machine config exists. Only one file is loaded;
settings and approval policies are not merged.

For a new local deployment, adapt [the optional repository example](examples/repository.toml) and save it as
`.engine/config.toml` (create `.engine` first). For an existing deployment,
copy its active config instead and add the repository and any verified command
approvals for your project, preserving the existing settings. Do not overwrite an existing
local config. The `.engine/` directory is ignored by Git.

The starter example has no active repository entries, command grants, or workflow
path overrides. Uncomment and customize the entries you need. Keep your existing
workflow definitions and other deployment settings.

For an existing checkout from any hosting provider:

```toml
[repos.my-project]
path = "/absolute/path/to/my-project"
```

The table key is the name shown in OpenEngine; `path` is the directory
containing your project's files on the host running OpenEngine. Add one entry
per repository.

To let OpenEngine clone a GitHub repository when its checkout is missing, use
its full GitHub name instead:

```toml
[repos."your-account/your-project"]
path = "~/code/your-project"
```

Replace `your-account` with the GitHub user or organization and `your-project`
with the repository name. The checkout path is yours to choose.

At web startup (including `engine-web --check`), a missing path whose name is
a GitHub `owner/repo` is cloned from `git@github.com:owner/repo.git`. This uses
the worker's existing SSH identity and the remote's default branch. Git and
working GitHub SSH access must already be installed on the host. No credentials
are created or changed. Cloning has a five-minute timeout and uses a temporary
sibling directory; failure stops startup with a configuration error and leaves
the configured destination absent so startup can be retried.

Existing paths are left untouched: no fetch, pull, reset or dependency install.
Local aliases such as `local-project` still require a pre-existing checkout; their remote
cannot be inferred. Other forges likewise need a pre-existing checkout. Paths
expand `~` and resolve relative to the server's working directory, not the
configuration file. The host needs write permission on the destination's parent.

When moving configuration into `.engine`, adjust config-relative paths:
for example `[workflows] directory = "../workflows"` loads the working
directory's workflows. State paths and the adjacent `.env` used for login
configuration also follow the selected config location; preserve the existing
deployment's locations when migrating. Repository checkout paths still resolve
against the server's working directory.

## Set up your repository

1. Verify that the account running OpenEngine can access the checkout.
   For automatic cloning, also verify GitHub SSH read access.
   Check write access separately if WorkOrders must push branches and open PRs.
2. Add `[repos."owner/repo"]` with `path = "/local/checkout/path"` in
   `.engine/config.toml`. Use the full GitHub name to enable automatic cloning of a missing path.
3. Read that repository's `AGENTS.md` and CI workflows. Add only the necessary
   commands to `[approvals.bash].allow`; avoid broad wildcard grants.
   These rules are deployment-wide, not scoped to a repository or working directory.
4. Update the host installation, run `engine-web --check` as the worker with the
   same config and working directory as the service, and restart the service.
   Check that the new checkout exists, select the repository in the web WorkOrder
   form, and create an editing/linting WorkOrder.

## Command approvals and rollout

Repository registration is optional. OpenEngine does not require any particular
repository or project toolchain. Configure only the repositories you want to use;
omit `[repos]` when none are needed.

The example has an empty shell allow list: all shell commands require approval
unless explicitly denied. No language or test runner is assumed. Before adding
a local grant, verify the project manifests, agent instructions, CI commands,
and required working directory. Keep test commands exact and target a dedicated
test directory. Avoid trailing wildcards that can match extra paths or shell
suffixes.
These rules match command text; they do not constrain the working directory or
what test code can execute.

Local deployment settings and access details belong on the host, outside version
control. If the service uses an external configuration, update that file or point
its `ENGINE_CONFIG` or `--config` at your local configuration. Preserve existing
settings when doing so. Merging code does not update or restart a running
instance: run the startup check, restart, and verify repository selection in the
live UI as part of rollout.

Registering a repository does not configure its GitHub event routing;
multi-repository webhooks are tracked in #709.

## Dedicated GitHub accounts and authenticated CLI onboarding

With GitHub login enabled, onboarding requires an operator browser session or a
separate `ENGINE_ONBOARDING_TOKEN`. Set this secret in the daemon environment or
in `.env` beside its selected `engine.toml`. Use at least 32 non-whitespace
characters and a different value from `ENGINE_SERVICE_TOKEN`. An unset token
disables bearer onboarding; using the service token as the onboarding token is
rejected. File-based secrets are reread on each request for rotation.

This is an operator credential: it explicitly permits cloning, rewriting repository
configuration, registering starter graphs and setting `trusted=true` through
onboarding. It authorizes only `POST /api/repositories/onboard` and
`GET /api/repositories/onboard/{job_id}`. Keep it out of agents and general service
clients. The general service token cannot onboard repositories or write repository
trust through `/api/v1/repositories/trust`; use the browser for later trust changes.
Host and browser Origin checks still apply.

Set the same onboarding secret in your CLI environment, then configure a dedicated
backend without changing the work-order backend:

```sh
engine backend add onboarding http://localhost:4364 --token-env ENGINE_ONBOARDING_TOKEN
engine repo add git@github.com:owner/repo.git --backend onboarding --review-backend local
```

Use your daemon's actual URL and port. Continue using the regular backend and
`ENGINE_SERVICE_TOKEN` for work orders.
Replace `local` with your regular backend for the same daemon. `--review-backend`
selects the backend in the printed review command; onboarding still uses
`--backend onboarding`. Without it, an onboarding-token backend prints setup
guidance instead of a review command that would fail authentication.

For a dedicated GitHub account, configure both the GitHub CLI login directory and
its SSH private key in the daemon's `engine.toml`:

```toml
[github]
gh_config_dir = "~/.config/gh-work"
ssh_key = "~/.ssh/id_ed25519_work"
```

`ssh_key` must name an existing file. It expands `~` and resolves relative paths
beside the configuration file. The daemon exports a shell-quoted
`GIT_SSH_COMMAND` with `IdentitiesOnly=yes` and `BatchMode=yes` before cloning;
workspace fetches and pushes inherit it. The key must work without an interactive
passphrase prompt. When omitted, the existing SSH environment is preserved.
Restart the daemon after changing these settings. Clone failures report git's
reason with URLs and embedded credentials redacted; publickey denials point to
`[github] ssh_key`, and timeouts are named explicitly.
