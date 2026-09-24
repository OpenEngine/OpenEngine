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
standalone ACP package), workflow definitions, license notices, and the manifest.
The web wheel includes the production React client and serves it when installed.
The manifest records the source commit, package versions, and SHA-256 and size
of every payload file. It does not hash itself. Third-party Python dependencies
are resolved at installation time; this is not an offline dependency mirror.

To build locally with Python 3.11+, uv, and Node 22:

```sh
npm --prefix apps/web ci
python scripts/build_release.py --commit <source-commit-sha>
```

To install from an extracted bundle:

```sh
uv pip install ./wheels/*.whl
```

Configure `engine.toml` for the deployment and point its workflow directory at
the extracted `workflows` directory. Provider CLIs and credentials are supplied
by the deployment.
