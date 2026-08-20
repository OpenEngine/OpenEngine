# Portable OpenEngine distribution

Status: in progress -- slice 1 has landed, and slice 2 for one platform

This plan turns the current source-checkout experience into an installable
product without creating a second way to build OpenEngine.

What exists today is one archive for linux-x86_64, built by
`packaging/build-archive.sh` and proved on a clean machine by
`.github/workflows/release-archive.yml`. `openengine doctor`, the remaining
platforms, the shell installer, Homebrew, and upgrades are all still ahead:
they consume this archive rather than change how it is made, which is the
sequencing this plan exists to keep. See "Delivery slices" below for what each
one still owes.

## User experience

The first portable release supports macOS on Apple silicon and Intel, plus
glibc-based Linux on x86-64. Linux on ARM64 follows once it has a native CI
runner. Windows users can use the Linux package under WSL; a native Windows
package is outside the first release.

The two supported installation paths are:

```bash
brew install spiralsoft-ai/tap/openengine
```

and:

```bash
curl -fsSL https://openengine.sh/install | sh
```

Both install an `openengine` command. Running `openengine` starts the local web
interface, while `openengine doctor` reports missing runtime integrations and
how to install them. The existing `engine-web`, `engine-worker`, and
`engine-control-server` commands remain available for operators and backwards
compatibility.

OpenEngine owns its Python runtime and web assets. Users do not need Python,
uv, Node.js, npm, or a repository checkout. Tools that OpenEngine orchestrates
remain external and independently authenticated:

- `git` is required for workspace management.
- `gh` is required for GitHub-backed source control.
- At least one supported agent CLI (`codex` or `claude`) is required to run an
  agent; both may be installed.

`openengine doctor` distinguishes required tools from optional integrations so
installation can succeed before the user chooses an agent provider.

## One artifact, two installers

Each release produces one self-contained archive for every supported OS and
architecture. The archive contains a standalone OpenEngine executable, the
built web client, licenses, and release metadata. It does not contain agent
CLIs or their credentials.

The release archive is the boundary all installers consume:

1. CI builds the web client once and embeds it as package data beside the web
   server module instead of relying on `apps/web/dist` in a checkout. Vite
   writes it to `apps/web/src/engine/apps/web/client`, and hatchling lists it
   as a build artifact so the wheel carries it despite it being git-ignored.
2. CI freezes the Python application and interpreter into a versioned archive.
3. CI runs the archive in a clean machine, calls `openengine doctor`, starts the
   server, and requests `/api/config`.
4. CI publishes the archives, a SHA-256 checksum manifest, and build
   attestations on a GitHub release.
5. The shell installer and Homebrew formula select and verify the matching
   archive rather than rebuilding the application.

This gives Homebrew and the shell installer identical application bits and
keeps Node and Python build tooling in CI.

The release matrix begins with:

| Target | CI smoke-test environment | First release |
| --- | --- | --- |
| macOS ARM64 | native Apple-silicon runner | yes |
| macOS x86-64 | native Intel runner | yes |
| Linux x86-64, glibc | oldest supported glibc image | yes |
| Linux ARM64, glibc | native ARM64 runner | after CI is available |
| Linux musl | none | no |
| Windows | WSL uses Linux artifact | native package deferred |

The build configuration records the minimum macOS and glibc versions. CI tests
those minimums, not only the newest hosted images.

## Command and data conventions

The portable CLI adds these stable commands before either installer ships:

- `openengine` and `openengine web` start the web interface.
- `openengine doctor` checks the platform, external commands, writable data
  directory, configuration, and whether at least one agent CLI is usable.
- `openengine --version` prints the application version and build commit.

Portable installs stop writing `conversations.sqlite3` in the launch directory.
Defaults follow the XDG base-directory convention on Linux and the equivalent
Application Support location on macOS. `ENGINE_CONFIG` and `--config` continue
to take precedence so existing deployments do not change behavior. Logs and
state are kept outside the installed version directory so upgrades are atomic
and rollbacks do not lose data.

The server binds to loopback by default. Installers do not create a background
service or open a browser in the first release; service management can be added
later as an explicit user choice.

## Shell installer contract

The install script is a small POSIX shell program served from the website and
versioned in this repository. It:

1. accepts `--version`, `--prefix`, and `--no-modify-path`;
2. normalizes only supported OS/architecture combinations and fails with a
   useful message for all others;
3. downloads the archive and checksum manifest from the corresponding GitHub
   release over HTTPS;
4. verifies the archive before extracting it;
5. installs into a versioned directory under `~/.local/openengine` by default
   and atomically updates `~/.local/bin/openengine`;
6. changes a shell profile only after explaining the change, and only when the
   bin directory is not already on `PATH`; and
7. prints the exact removal instructions and recommends `openengine doctor`.

It never requires `sudo`, executes an unverified download, collects telemetry,
or installs an agent CLI. Re-running the same version is idempotent. Installing
a new version preserves the preceding version for rollback, and an interrupted
download cannot replace the working command.

The repository tests the script against a local fake release server, including
checksum failure, unsupported platforms, spaces in the install path,
idempotency, upgrade, rollback, and a non-interactive shell.

## Homebrew contract

The project maintains `spiralsoft-ai/homebrew-tap`. Its `openengine` formula
contains the release URLs and checksums generated by the release workflow. The
formula installs the same archives as the shell installer and exposes
`openengine` through Homebrew's normal prefix.

The formula has no Python or Node build dependency. It declares only runtime
dependencies that are truly universal; optional agent CLIs are reported by
`openengine doctor`, not forced on every user. Formula tests run
`openengine --version`, `openengine doctor --format json`, start the server on
an ephemeral port, and request its health endpoint.

Formula updates are opened automatically only after all release-archive smoke
tests pass. Publishing the GitHub release remains separate from merging the tap
update, so a broken formula cannot silently become the only installation path.

## Delivery slices

Each slice is independently releasable and has a focused acceptance test.

### 1. Define the installed application -- done, less `doctor`

- Add the `openengine` CLI with `web`, `doctor`, and `--version`.
  `doctor` is outstanding; `web` and `--version` are in.
- Move the built frontend under the Python package and include it in wheels.
- Resolve configuration and mutable data outside the installation directory.
- Build a wheel, install it into a clean environment, start it outside the
  checkout, and exercise `/api/config` in CI.

Done means a source tree and Node.js are no longer runtime requirements.

### 2. Produce portable release archives -- done for linux-x86_64

- Choose and pin the freezing tool and standalone Python version.
  A standalone CPython from `uv python install`, with the application
  installed into it and a launcher that runs it by relative path. Both the
  interpreter version and uv are pinned.
- Build the initial platform matrix from tags after the existing release gate.
  One platform so far. `packaging/build-archive.sh` detects the host, so the
  remaining targets are runners rather than new code -- but each one still
  owes a clean-machine test on its *minimum* platform, which is the part that
  needs thought rather than a matrix entry.
- Generate checksums, software-bill-of-materials metadata, and attestations.
  Checksums and signed provenance are published; an SBOM is not
  (`uv export --format cyclonedx1.5` is the obvious source).
- Smoke-test each final archive on its minimum supported platform.
  Tested on Debian 12, which is newer than the glibc the build actually
  requires. The minimum is not yet pinned or tested.

Done means a downloaded archive can run on a clean supported host with no
system Python.

### 3. Ship the shell installer

- Implement the installer contract and its failure-path test matrix.
- Publish the versioned script and stable website redirect.
- Document install, upgrade, rollback, uninstall, and offline verification.

Done means a clean host can install and remove OpenEngine without a checkout or
administrator privileges.

### 4. Ship Homebrew

- Create the tap and formula from the tested release metadata.
- Add formula installation tests on both macOS architectures.
- Automate formula-update pull requests from successful releases.

Done means `brew install spiralsoft-ai/tap/openengine` installs the same tested
bits as the shell installer.

### 5. Harden and expand

- Add startup migration and rollback tests for persisted state.
- Establish support windows and a release-retention policy.
- Add Linux ARM64 when a native build and minimum-platform test are available.
- Evaluate a native Windows package from usage feedback rather than making WSL
  support block the initial release.

## Release criteria

A version is portable only when all of these are true:

- No build tool or repository checkout is required on a supported host.
- The packaged UI and API work when launched from an arbitrary directory.
- `openengine doctor` identifies every missing external runtime dependency.
- Archive checksum verification and clean-host smoke tests pass for every
  advertised target.
- Shell and Homebrew installations report the same version and build commit.
- Upgrade, rollback, and uninstall preserve user data.
- Installation does not require elevated privileges or modify shell startup
  files without notice.

Until those criteria pass, the README continues to label the uv/npm workflow as
a source installation rather than presenting it as the portable install path.
