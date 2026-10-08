# Onboarding a repository

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

Add any repository under `[repos]`, for example:

```toml
[repos]
"owner/repo" = "~/code/repo"
```

At web startup (including `engine-web --check`), a missing path whose name is
a GitHub `owner/repo` is cloned from `git@github.com:owner/repo.git`. This uses
the worker's existing SSH identity and the remote's default branch. Git and
working GitHub SSH access must already be installed on the host. No credentials
are created or changed. Cloning has a five-minute timeout and uses a temporary
sibling directory; failure stops startup with a configuration error and leaves
the configured destination absent so startup can be retried.

Existing paths are left untouched: no fetch, pull, reset or dependency install.
Local aliases such as `n8n` still require a pre-existing checkout; their remote
cannot be inferred. Other forges likewise need a pre-existing checkout. Paths
expand `~` and resolve relative to the server's working directory, not the
configuration file. The host needs write permission on the destination's parent.

When moving configuration into `.engine`, adjust config-relative paths:
for example `[workflows] directory = "../workflows"` loads the working
directory's workflows. State paths and the adjacent `.env` used for login
configuration also follow the selected config location; preserve the existing
deployment's locations when migrating. Repository checkout paths still resolve
against the server's working directory.

## Repeat for another repository

1. Verify repository access as the worker account, including SSH read access.
   Check write access separately if WorkOrders must push branches and open PRs.
2. Add `"owner/repo" = "/local/checkout/path"` under `[repos]` in
   `.engine/config.toml`. Use the full GitHub name to enable automatic cloning of a missing path.
3. Read that repository's `AGENTS.md` and CI workflows. Add only the necessary
   commands to `[approvals.bash].allow`; avoid general `uv **` or `npm **` grants.
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
