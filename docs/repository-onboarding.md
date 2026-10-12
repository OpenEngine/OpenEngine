# Onboarding a repository

Run `engine onboard https://github.com/owner/repo` and choose connected or
disconnected mode. Use `--mode connected` (or `disconnected`) to skip the prompt,
and `--path /absolute/checkout` to choose a destination instead of `~/code/repo`.
With no URL, `engine onboard` registers the current GitHub checkout with a local daemon.

The web UI offers **Add repository** on the WorkOrder form and on first run.
MCP clients can call `onboard_repository(repository, path?, mode?)`, then poll
`repository_onboarding_status(job_id)` using the returned `id`. All three surfaces
use the same daemon job and display its progress and actionable failures.

The daemon account needs git, working GitHub SSH access (`ssh -T git@github.com`),
and a signed-in agent (`engine agent signin claude`, `codex`, or `opencode`).
Preflight failures leave the checkout and configuration untouched. Onboarding
never changes credentials. Existing checkouts of the same remote are reused.
Repeating successful onboarding reports that the repository is already onboarded.

Onboarding preserves existing settings and comments in the configuration the
daemon loaded (`--config` / `ENGINE_CONFIG` when selected). Without a loaded file,
it creates `$XDG_CONFIG_HOME/openengine/engine.toml`, defaulting to
`~/.config/openengine/engine.toml`. It updates the running daemon immediately and
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
[repos]
"my-project" = "/absolute/path/to/my-project"
```

The left side is the name shown in OpenEngine; the right side is the directory
containing your project's files on the host running OpenEngine. Add one entry
per repository.

To let OpenEngine clone a GitHub repository when its checkout is missing, use
its full GitHub name instead:

```toml
[repos]
"your-account/your-project" = "~/code/your-project"
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
2. Add `"owner/repo" = "/local/checkout/path"` under `[repos]` in
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
