# Onboarding a repository

`[repos]` in the active `engine.toml` supplies the repository choices in the
WorkOrder form. PATY is registered as:

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

## Repeat for another repository

1. Verify repository access as the worker account, including SSH read access.
   Check write access separately if WorkOrders must push branches and open PRs.
2. Add `"owner/repo" = "/local/checkout/path"` under `[repos]` in the **active**
   config. Use the full GitHub name to enable automatic cloning of a missing path.
3. Read that repository's `AGENTS.md` and CI workflows. Add only the necessary
   commands to `[approvals.bash].allow`; avoid general `uv **` or `npm **` grants.
   These rules are deployment-wide, not scoped to a repository or working directory.
4. Update the host installation, run `engine-web --check` as the worker with the
   same config and working directory as the service, and restart the service.
   Check that the new checkout exists, select the repository in the web WorkOrder
   form, and create an editing/linting WorkOrder.

## PATY commands and deployment handoff

The committed approvals include the two required `AGENTS.md` lint commands:

```bash
uv run ruff check agent/ pipecat_outbound/
uv run --directory mcp ruff check src/
```

The existing `uv run pytest **` rule also matches bare `uv run pytest` and PATY's
test-directory variants. Exact sync, lint and format-check commands come from
`tests.yml`, `pipecat_outbound_tests.yml`, `cli_tests.yml` and `ruff.yml`. Commands
without `--directory` run in the working directory indicated by PATY's workflow
(root, `mcp`, or `cli`). Mini-app install/build/test commands and
`node test-local.mjs` come from `AGENTS.md` and `ui-tests.yml` and run in `mcp/ui`.
System package/browser installation, live telephony, MCP connection setup,
deployment/release workflows and the separate mobile toolchain are outside this
onboarding's explicit command additions. The deployment's existing
`auto_approve = true` setting is unchanged; these patterns also work when it is
disabled and do not constitute sandbox isolation.

During issue #710 onboarding, the existing `OpenEngine-worker` account could
read the private repository and clone it over SSH into `~/code/PATY` without
credential changes. GitHub reported `pull: true`, **`push: false`**. Editing and
local checks are possible; a maintainer must resolve publishing permissions
before expecting WorkOrders to push PATY branches. No credentials or GitHub
connections were changed.

The worker session selected an external config via
`ENGINE_CONFIG=/Users/openengine/.config/openengine/engine.toml`. A merged change
to this repository's `engine.toml` does not update that file. A host operator must
copy the PATY repo entry and approval additions into the deployment's active
config, update the installed code, run `engine-web --check`, and restart it.
The live UI acceptance check remains part of that rollout; merging does not
update or restart the running instance.

PATY end-to-end execution is not part of onboarding: portaudio, mlx and Daily
credentials may be unavailable in agent workspaces. Registering PATY does not
enable its GitHub event routing; multi-repository webhooks are tracked in #709.
