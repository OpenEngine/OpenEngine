# Releases

[Release Please](https://github.com/googleapis/release-please) opens a version and
changelog PR from Conventional Commits on `main`. All Python distributions share
one version, tracked in `.release-please-manifest.json`; the config's extra files
keep the workspace packages, standalone `langgraph-acp`, and both uv lockfiles
in sync. Add new
Python distributions to that config when adding them to the workspace.

The release workflow calls the release gate before creating a release: the
most recent scheduled `ACP compatibility` run on the default branch must be
green and no more than eight days old. Merging the release PR creates a `v<version>` GitHub release and builds
its exact commit. The same bundle build runs on pull requests. Enable “Allow
GitHub Actions to create and approve pull requests” in repository settings.
The default GitHub token does not trigger CI on bot-created release PRs; run the
`tests` workflow manually on the release PR branch before merging it.

Each release attaches `openengine-<version>.tar.gz` and `release-manifest.json`.
The archive contains all first-party wheels (including migrations and the
standalone ACP package), workflow definitions, a default `engine.toml`, a
hash-locked `requirements.txt`, license notices, and the manifest.
`requirements.txt` pins every third-party Python dependency exported from
`uv.lock` and every first-party wheel in the bundle, each with its SHA-256. The web wheel includes the
production React client and serves it when installed. The manifest records the
source commit, package versions, the requirements and config files, and SHA-256
and size of every payload file. It does not hash itself. The copy attached to
the release also records `archive_sha256`, the digest of the archive. Third-party packages are
downloaded at installation time, but only at the pinned versions and hashes;
this is not an offline dependency mirror.

To build locally with Python 3.11+, uv, and Node 22:

```sh
npm --prefix apps/web ci
python scripts/build_release.py --commit <source-commit-sha>
```

## One-line install

On macOS or Linux (x86_64 or arm64), with only `curl` and `tar`:

```sh
curl -LsSf https://openengine.sh/install.sh | sh
```

`scripts/install.sh` is published on the site and attached to each release. It
downloads a pinned uv into `~/.local/share/openengine/bin`, isolated from any
uv configuration on the machine, fetches the latest release (or `--version
X.Y.Z`), checks the archive against the `archive_sha256` in the published
`release-manifest.json` before extracting it to `versions/<version>`, and
installs it into a venv on uv's own Python 3.12 with `--require-hashes`. A
`current` symlink points at that version; `~/.local/bin/engine` runs the
terminal client and manages the web service through `engine daemon`.
`~/.config/openengine/engine.toml` is written from the bundled default,
keeping state in `~/.local/state/openengine`, only when it does not already
exist. The `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
`XDG_CACHE_HOME`, and `XDG_BIN_HOME` directories are honoured, and `--prefix DIR`
replaces the data directory. It then runs `engine daemon setup`, which starts
OpenEngine in the background and opens it in a browser; `--no-start` and
`--no-browser` skip those. Running it again reuses an installed version and
never touches the config or state. The release workflow runs it on clean Ubuntu
and macOS runners against the bundle it just built (`OPENENGINE_RELEASE_URL`
names that directory), checks `/api/health`, and then runs `engine daemon setup
--no-browser`, `status`, and `stop`.

## Background service

`engine daemon setup` records the absolute paths of `git`, `node`, `npx`,
`claude`, and `codex`, then registers a per-user LaunchAgent
(`~/Library/LaunchAgents/sh.openengine.engine.plist`) on macOS or a systemd user
unit (`~/.config/systemd/user/openengine.service`) on Linux, and starts it.
Where neither is usable, the service is a detached process tracked by a pidfile.
The service runs `engine-web` only (never `engine-orchestrator`; Temporal is not
used) with `ENGINE_CONFIG` set, bound to `127.0.0.1`, and with a `PATH` built
from the recorded tool directories rather than the shell's. Rerun setup after
installing a new tool or version. There is one instance per user; logs go to
`~/.local/state/openengine/logs`.

```sh
engine daemon          # start if needed, wait for /api/health, open the browser
engine daemon start    # start without opening a browser
engine daemon stop     # SIGTERM, waiting for a graceful shutdown
engine daemon status   # version, health, and URL (--json)
engine daemon logs -f
engine daemon doctor   # config, port, and git/node/npx/claude/codex
```

## Manual install

To install from the extracted bundle directory:

```sh
uv pip install --require-hashes -r requirements.txt
engine-web --config ./engine.toml
```

The wheels are installed through `requirements.txt` rather than named on the
command line, because hash-checking mode refuses a wheel given without a hash.

No source checkout is needed, and the server can be started from any directory.
The bundled `engine.toml` listens on `127.0.0.1:4364`, keeps its databases in
`state/` beside itself, and reads the bundled `workflows` directory. Relative
paths resolve against the config file's directory. `[server]` sets `host` and
`port`; `[state]` sets `directory`, and `sqlite_path` and
`graph_state_directory` within it. `ENGINE_HOST`, `ENGINE_PORT`,
`ENGINE_STATE_DIRECTORY`, `ENGINE_SQLITE_PATH`, and
`ENGINE_GRAPH_STATE_DIRECTORY` override them. The server refuses to start on a
host other than loopback unless GitHub login is configured. The Temporal orchestrator is not
started or needed. The deployment supplies Node.js with `npx`,
which Engine uses to launch the pinned ACP adapters, and each provider's
credentials. No `codex` or `claude` executable is needed.
