# Onboarding a repository

Use the untracked `.engine/config.toml` in the server's working directory to
configure repositories and approvals without editing the committed `engine.toml`.
Configuration selection is: explicit `--config`, then `ENGINE_CONFIG`, then the machine config
(`$XDG_CONFIG_HOME/openengine/engine.toml`, defaulting to
`~/.config/openengine/engine.toml`), then `.engine/config.toml`, then the root
`engine.toml`. Use `--config .engine/config.toml` to explicitly select local
settings when a machine config exists. Only one file is loaded;
settings and approval policies are not merged.

For a new local deployment, copy [the PATY example](examples/paty.toml) to
`.engine/config.toml` (create `.engine` first). For an existing deployment,
copy its active config instead and add the repository and command approvals
from the example, preserving the existing settings. Do not overwrite an existing
local config. The `.engine/` directory is ignored by Git.

Add any repository under `[repos]`, for example:

```toml
[repos]
"spiralsoft-ai/PATY" = "~/code/PATY"
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

## PATY commands and deployment handoff

The PATY example includes the two required `AGENTS.md` lint commands:

```bash
uv run ruff check agent/ pipecat_outbound/
uv run --directory mcp ruff check src/
```

The example approves only exact pytest commands targeting the dedicated
`tests/` directory or its named suites (`tests/unit`, `tests/http`,
`tests/simulator`, and `tests/smoke`). The listed flags match PATY's workflows.
Bare pytest, other paths, extra arguments, and chained shell commands require
approval. Add further test commands as exact entries, never a trailing wildcard.
These rules restrict command text, not the process working directory or what
test code can execute. Exact sync, lint and format-check commands come from
`tests.yml`, `pipecat_outbound_tests.yml`, `cli_tests.yml` and `ruff.yml`. Commands
without `--directory` run in the working directory indicated by PATY's workflow
(root, `mcp`, or `cli`). Mini-app install/build/test commands and
`node test-local.mjs` come from `AGENTS.md` and `ui-tests.yml` and run in `mcp/ui`.
System package/browser installation, live telephony, MCP connection setup,
deployment/release workflows and the separate mobile toolchain are outside this
onboarding's explicit command additions. The example leaves auto-approval disabled; the listed commands are allowed
explicitly. These patterns do not constitute sandbox isolation.

During issue #710 onboarding, the existing `OpenEngine-worker` account could
read the private repository and clone it over SSH into `~/code/PATY` without
credential changes. GitHub reported `pull: true`, **`push: false`**. Editing and
local checks are possible; a maintainer must resolve publishing permissions
before expecting WorkOrders to push PATY branches. No credentials or GitHub
connections were changed.

The worker session selected an external config via
`ENGINE_CONFIG=/Users/openengine/.config/openengine/engine.toml`. That override
continues to win over local discovery. A host operator must prepare
`.engine/config.toml` with the existing deployment settings and PATY additions,
then point the service's `ENGINE_CONFIG` at it (or remove that override and start
from the directory containing `.engine`). Update the installed code, run
`engine-web --check` with that configuration, and restart it.
The live UI acceptance check remains part of that rollout; merging does not
update or restart the running instance.

PATY end-to-end execution is not part of onboarding: portaudio, mlx and Daily
credentials may be unavailable in agent workspaces. Registering PATY does not
enable its GitHub event routing; multi-repository webhooks are tracked in #709.
