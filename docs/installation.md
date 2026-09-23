# Standalone installation

Download the `openengine-<platform>` artifact from the **standalone release**
workflow for the desired version/commit. Extract the artifact, then its `.tar.gz`.
The initial supported targets are macOS 15+ on Apple Silicon (`Darwin-arm64`)
and Ubuntu 24.04+ on x86-64 (`Linux-x86_64`). Other distributions/architectures
are not yet tested. Each artifact includes `smoke-result.json` recording the
architecture, source commit and acceptance test result.

From any directory:

```sh
sh /absolute/path/to/release/install.sh
~/.local/share/openengine-release/bin/engine-web
```

Visit http://127.0.0.1:8000. Startup stays in the foreground; Ctrl-C stops it.
Optionally add `~/.local/share/openengine-release/bin` to PATH. Pass a different
absolute installation directory as the installer's first argument. Existing
installation directories are refused; upgrade orchestration is out of scope.
Installation needs only a POSIX shell, tar, and `sha256sum` (Linux) or `shasum`
(macOS). Python, uv, Node.js, npm, compilers and a source checkout are unnecessary.
After downloading the artifact, installation is offline.

The bundle contains pinned uv and CPython 3.12.12, non-editable wheels for the
application and all dependencies, frontend assets, Alembic histories, and the
default implementation/review workflow. The installer verifies SHA-256 checksums,
then uses private uv to create an isolated environment with the bundled runtime
and install only hashed binary wheels. `manifest.json` records the application
version, source commit, platform, dependency versions and runtime artifacts.
`SHA256SUMS` also covers the manifest. Checksums detect corruption; download
artifacts from a trusted workflow run (they are not independent signatures).

The UI starts without agent CLIs or credentials. Using agent capabilities
requires the corresponding Claude/Codex installation and authentication;
graph agents additionally use their version-pinned ACP adapters through
Node.js/npm. Git repository operations need git and GitHub operations need gh
and its authentication. These are optional capability prerequisites, not
components launched for default UI startup. The release does not install or
authenticate Claude or Codex, run Temporal, or register a background service.
Missing executables are reported by the requested capability.

## Configuration and data

Web startup ignores `engine.toml` and `.env` in the current working directory.
It reads the per-user `engine.toml` when present, or neutral built-in defaults.
Use `engine-web --config /path/to/engine.toml` or `ENGINE_CONFIG` to select an
explicit file. A `[workflows] directory = "/path/to/workflows"` entry replaces
the bundled workflow; relative paths are resolved beside the selected config.
Deployment URLs, authentication and repository paths must be explicitly supplied.

| Location | Linux | macOS |
| --- | --- | --- |
| Configuration | `~/.config/openengine/engine.toml` | `~/Library/Application Support/openengine/engine.toml` |
| Databases and workspaces | `~/.local/share/openengine/` | `~/Library/Application Support/openengine/` |
| Logs | `~/.local/state/openengine/log/` | `~/Library/Logs/openengine/` |

Linux follows XDG overrides. `ENGINE_DATA_DIR` overrides the database/workspace
root on both platforms; use an absolute path. `ENGINE_PORT` changes the HTTP port.
The server binds loopback. Logs rotate, and mutable state lives outside the
installed release. Restarting from another directory uses the same state.
Configuration-local secrets remain beside an explicit config file, or in the
per-user configuration directory by default.

## Release validation

CI builds the frontend once, exports the frozen `uv.lock` dependency closure,
builds version-stamped workspace wheels, and downloads only third-party binary
wheels on each target. It provisions the pinned interpreter and uv in the bundle.
The native macOS/Linux smoke jobs hide the checkout and restrict PATH to shell
utilities. They install offline into a path containing spaces, start HTTP from
an unrelated directory, fetch frontend assets and API configuration, load the
default workflow, upgrade older state/graph schemas, exercise explicit config
and workflow overrides, and restart elsewhere to verify persisted conversation
state. Artifacts are uploaded only after these checks pass. The release pipeline
also runs on pull requests; tag `vMAJOR.MINOR.PATCH` for versioned bundles.
