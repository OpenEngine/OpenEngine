# Install a packaged release

Download the archive and matching `.sha256` file for your machine from the
**packaged release** CI run (the `openengine-<platform>` artifact). Tagged runs
use the tag version (`v1.0.0` → `1.0.0`); PR/manual runs use `0.0.0` and record
the exact source commit in `manifest.json`.

Supported and tested targets:

| Artifact | CI host | Architecture |
| --- | --- | --- |
| `openengine-linux-x86_64.tar.gz` | Ubuntu 24.04, glibc | x86_64 |
| `openengine-darwin-arm64.tar.gz` | macOS 14 | Apple Silicon |

Other architectures, musl Linux, native Windows and WSL provisioning are not
part of this release. No Python, Node.js, npm, uv, compiler, or source checkout
is needed to install or start the UI. The installer uses the system shell,
standard file utilities, and `sha256sum` (Linux) or `shasum` (macOS).

```sh
# Substitute your downloaded platform filename.
shasum -a 256 -c openengine-darwin-arm64.tar.gz.sha256
# Linux can use sha256sum -c instead.
tar -xzf openengine-darwin-arm64.tar.gz
sh bundle/install.sh
~/.local/bin/engine-web
```

Visit `http://127.0.0.1:8000`. Startup is foreground only; Ctrl-C stops it.
`engine-web --port 8080` selects another loopback port. Add `~/.local/bin` to
PATH to invoke `engine-web` by name. Installation does not start a service,
launch a browser, or authenticate agent CLIs.

Installation is offline: the archive includes uv 0.12.7, its standalone
CPython 3.12.12 runtime, and a native wheelhouse. uv provisions an isolated
virtual environment using that private runtime and installs only hash-pinned
binary wheels, with network access and source builds disabled. Application
wheels all carry the release version. The frontend was built once in CI.
`manifest.json` records the source commit, platform/architecture, runtime
versions and provisioning, complete dependency set, and file checksums;
`SHA256SUMS` also covers the manifest and installer. Obtain both archive and
checksum from the trusted CI run; checksums detect corruption, not publisher
identity.

The release goes in `~/.local/share/openengine/releases/<version>-<platform>`.
Set absolute `ENGINE_INSTALL_DIR` and `ENGINE_BIN_DIR` to choose different
release and command locations. Existing release directories are never
replaced. Keep the installed release at its original location: virtualenv
entrypoints contain that path. The extracted download can be removed after
installation. Upgrade orchestration, rollback and service registration are
separate work.

## Configuration and state

The UI starts with portable defaults and no login requirement. It binds to
IPv4 loopback, has no preconfigured repositories, public URL, Slack channel,
or OAuth deployment settings, and offers the bundled implementation/review
workflow. Agent executables and credentials are checked when used, so their
absence does not prevent UI startup. Install/authenticate Claude or Codex
separately to run agents. Repository capabilities need Git and the appropriate
forge CLI/authentication; ACP workflow providers additionally need Node/npm
for their existing `npx` adapters. These optional capability prerequisites are
not installed or authenticated by this installer. Temporal is not started by
`engine-web`; the default workflows use embedded SQLite/LangGraph.

| Files | Linux default | macOS default |
| --- | --- | --- |
| `engine.toml`, adjacent `.env` | `~/.config/openengine/` | `~/Library/Application Support/openengine/` |
| `conversations.sqlite3`, `graph-state/`, `workspaces/` | `~/.local/share/openengine/` | `~/Library/Application Support/openengine/` |
| `engine-web.log` (rotating) | `~/.local/state/openengine/log/` | `~/Library/Logs/openengine/` |

Linux respects the corresponding XDG directory variables. `ENGINE_CONFIG_DIR`,
`ENGINE_DATA_DIR` and `ENGINE_LOG_DIR` override these locations; use absolute
paths. Mutable files stay outside the installed release and survive starting
from a different directory. Existing checkout-local databases are not moved
automatically: stop the old server and copy its `conversations.sqlite3` and
`graph-state/` into the data directory before starting the installed release.

Configuration selection is `--config FILE`, then `ENGINE_CONFIG`, then the
per-user `engine.toml`, then built-in defaults. The web app does not implicitly
load a file from cwd. Explicit files retain relative-path semantics:

```toml
[repos]
my-project = "/absolute/path/to/project"

[workflows]
directory = "my-workflows" # relative to this configuration file
```

Custom workflow directories replace the bundled catalog and contain trusted
Python definitions. Omit `[workflows]` to use the bundled default. Databases
are upgraded on startup using packaged Alembic histories; LangGraph manages
its own checkpoint schema.

## Build and acceptance checks

`.github/workflows/release.yml` builds the frontend once, then builds all
workspace wheels plus `langgraph-acp`, and downloads only wheels from the
committed `uv.lock` for each native platform. A missing compatible wheel fails
CI. `release/build.py` takes an explicit application version and source SHA.
It stages version changes without modifying source project versions.

Each native job hides the checkout and runs `release/smoke.py` against the
artifact. The test supplies an empty home and a PATH containing only installer
utilities. It installs offline, loads the frontend and API, discovers the
default workflow, checks fresh and previous-schema database migration,
creates a conversation, restarts from another cwd, and checks explicit
configuration/custom workflow overrides. Only tested archives are uploaded.
