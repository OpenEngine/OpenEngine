# Install a release

Download the tested `openengine-<platform>.tar.gz` artifact from the **installable
release** GitHub Actions run for the desired commit or version tag. Extract it,
then run its installer (no root privileges):

```sh
tar -xzf openengine-aarch64-apple-darwin.tar.gz -C /path/to/empty-directory
sh /path/to/empty-directory/install.sh
~/.local/bin/engine-web
```

Open `http://127.0.0.1:8000`. Startup is foreground-only; Ctrl-C stops it.
Installation does not start the server or open a browser. Add `~/.local/bin` to
PATH if you want to run `engine-web` without its full path. You can start from
any directory and delete the extracted artifact after installation.

Supported and tested targets are **macOS 14+ Apple Silicon (arm64)** and
**Linux x86_64 with glibc 2.35+** (CI uses Ubuntu 24.04). Intel macOS, Linux ARM,
musl, and native Windows are not currently release targets. WSL provisioning
will be handled separately.

The archive includes uv 0.9.28, every application and dependency wheel, built
frontend assets, Alembic histories, and the implementation/review workflow.
The installer verifies SHA-256 checksums, uses uv to download a private CPython
3.12.12 runtime, and installs only the included hashed wheels without accessing
a package index. It needs an internet connection for the Python download and
standard POSIX shell tools (including `shasum` or `sha256sum`), but no existing
Python, Node.js, npm, compiler, or source checkout. The bundled uv pins the
Python distribution download metadata and verifies the runtime download.

The private runtime and application environment live under
`~/.local/share/openengine/runtime`. `OPENENGINE_INSTALL_DIR` and
`OPENENGINE_BIN_DIR` override the installation and command directories.
An existing environment is refused; upgrading, backup, rollback, background
services and automatic startup are outside this release's installer scope.

`manifest.json` records the application version, source commit, exact wheel
set, Python and uv pins, tested platform/architecture, and artifact hashes.
`SHA256SUMS` also covers the manifest. Checksums detect corruption; obtain the
archive from a trusted CI run. Local packages all receive the release version;
PR builds use `0.0.0+<commit>`, while `v1.2.3` tags produce `1.2.3`.

## Configuration and state

No personal repository, public URL, organization channel, OAuth deployment or
agent credentials are configured by default. The server binds to loopback.
Claude/Codex are optional at startup: install and authenticate them separately
when you want to use their capabilities. Missing executables or credentials
are reported by the relevant runner. Git and forge CLIs are likewise required
only for their repository capabilities. Default UI startup runs SQLite and
LangGraph in process; no Temporal server or other runtime-managed executable
is downloaded at startup (`startup_components` is empty in the manifest).

| Location | Linux | macOS |
| --- | --- | --- |
| Configuration | `~/.config/openengine/engine.toml` | `~/Library/Application Support/openengine/engine.toml` |
| Databases | `~/.local/share/openengine/` | `~/Library/Application Support/openengine/` |
| Logs | `~/.local/state/openengine/log/` | `~/Library/Logs/openengine/` |

Linux paths honor the corresponding XDG environment variables. Conversation
state is `conversations.sqlite3`; graph state is in `graph-state/`. Logs rotate.
These locations do not depend on the working directory and are outside the
installed environment. Configuration is optional; an absent file uses minimal
built-in defaults. To customize it, create `engine.toml` in the configuration
location, for example:

```toml
show_projects = true

[workflows]
directory = "/absolute/path/to/my/trusted/workflows"
```

`engine-web --config /path/to/engine.toml` takes precedence over `ENGINE_CONFIG`,
which takes precedence over the per-user file. Relative workflow directory
paths are resolved beside the selected configuration file. Without an override,
the bundled workflow is used. An incidental `engine.toml` in the current
working directory is not loaded; source developers can select the repository
configuration explicitly with `--config`. `.env` secrets are read beside the
selected config (or in the per-user config directory).

## Release validation

The release workflow builds the frontend once, assembles platform wheelhouses
from `uv.lock`, and tests the actual installer on both target architectures.
It hides the checkout and removes language tools and agent CLIs from PATH,
installs into a fresh home, starts the real HTTP server, fetches frontend assets
and API configuration, checks workflow compilation, upgrades an earlier schema
with preserved data, restarts from a different directory, and checks explicit,
environment, and user configuration selection plus custom workflows.
Only artifacts from a successful run should be distributed.
