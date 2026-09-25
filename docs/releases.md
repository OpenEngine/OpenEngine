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
and size of every payload file. It does not hash itself. Third-party packages are
downloaded at installation time, but only at the pinned versions and hashes;
this is not an offline dependency mirror.

To build locally with Python 3.11+, uv, and Node 22:

```sh
npm --prefix apps/web ci
python scripts/build_release.py --commit <source-commit-sha>
```

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
