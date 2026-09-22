# Install a packaged release

Download the matching `openengine-<platform>.tar.gz` and `SHA256SUMS` from the
GitHub release. Verify the archive against the published checksum, extract it,
and run:

```sh
tar -xzf openengine-darwin-arm64.tar.gz # Linux: openengine-linux-x86_64.tar.gz
sh release/install.sh
~/.local/bin/engine-web
```

Open `http://127.0.0.1:8000` (use `--port` to change the port). Startup is foreground; Ctrl-C stops the application.
The installer does not start it, open a browser, or register a service. Add
`$HOME/.local/bin` to PATH to invoke `engine-web` directly from any directory.

Supported and artifact-tested targets are macOS 15+ on Apple Silicon (arm64),
and Linux x86_64 with glibc (CI: Ubuntu 24.04). Native Windows, Intel macOS,
Linux ARM, musl distributions, and WSL provisioning are not covered here.

No existing Python, Node.js, npm, uv, or source checkout is required. A POSIX
shell and `sha256sum` or `shasum` are required. The bundle contains uv 0.12.7;
it downloads managed CPython 3.14.7 into OpenEngine's private data directory
(internet access is required for this step). All application dependencies are
installed offline from hash-locked wheels. No editable installation, frontend
build, or source compilation happens at installation time. Keep the downloaded
release from a trusted source: checksums detect corruption, not an untrusted
publisher.

The bundled implementation/review workflow loads without agent credentials.
Claude/Codex and their credentials are optional for opening the UI; the relevant
agent capability reports a missing CLI when used. Running graph agents also
requires the selected provider's ACP adapter tooling (currently Node/npx).
These optional tools are not installed or authenticated by this installer.
No external server or runtime-managed daemon is required for default web startup:
the graph runtime uses bundled SQLite dependencies. The private Python runtime
and its provisioning command are recorded in the manifest.

## Configuration and data

| Location | macOS | Linux |
| --- | --- | --- |
| Configuration | `~/Library/Application Support/OpenEngine/engine.toml` | `~/.config/OpenEngine/engine.toml` |
| Databases and releases | `~/Library/Application Support/OpenEngine/` | `~/.local/share/OpenEngine/` |
| Logs | `~/Library/Logs/OpenEngine/` | `~/.local/state/OpenEngine/log/` |

Linux locations respect the standard XDG environment variables. `conversations.sqlite3`
and `graph-state/` contain application state. `releases/<version>/` contains
immutable application files; `python/` holds managed Python. Logs rotate at 5 MB
with three backups. Foreground server output also remains available in the terminal.
Credentials retain the existing per-user credential-store behavior.

With no configuration file, portable built-in defaults apply: loopback binding,
no repository, public URL, organization channel, or deployment authentication.
Web startup ignores `engine.toml` and `.env` in the working directory.
Use `engine-web --config /path/to/engine.toml` or `ENGINE_CONFIG` for an explicit
configuration (relative explicit paths resolve from the caller's directory).
A `.env` alongside that configuration retains its existing secret behavior.
A custom workflow directory overrides the bundled workflow:

```toml
[workflows]
directory = "my-workflows" # relative to this configuration file
```

Existing checkout users should explicitly select their checkout's `engine.toml`.
Copying older checkout-local databases into the user data location is a manual
migration; upgrade orchestration, backups, rollback, and services are separate work.
The installer refuses an already-installed version rather than modifying it.

## Release production

The `packaged release` workflow builds the frontend once, builds versioned
workspace wheels and a native wheelhouse from `uv.lock`, then tests each bundle
on a separate runner without a checkout or host runtimes on PATH. Tests initialize
fresh databases, migrate populated historical schemas, load frontend/API/workflows,
exercise configuration overrides, and restart in another directory to check state.
Tag `vX.Y.Z` to publish tested archives; PRs produce development artifacts.

Each bundle includes `manifest.json` (application version, commit, exact Python/uv
versions, platform/architecture, dependency versions and SHA-256 checksums),
`requirements.txt` (the tested wheel hashes), and `SHA256SUMS`. The release page
also checksums the archives. Build scripts never resolve a replacement dependency
set on the user's machine.
