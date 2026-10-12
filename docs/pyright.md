# Workspace type checking

From the repository root:

```sh
uv sync --locked --all-packages
uv run --locked pyright
```

The dependency-boundaries CI job runs the same check. Pyright is pinned in the
root dev group; upgrades must recheck the baseline. `pyrightconfig.json` enables
standard mode for every `engine` namespace source tree under apps, CLI and packages,
plus `tests/typecheck`. Explicit `extraPaths` join the namespace packages for
static import resolution; add an entry when adding a distribution. Dependencies
are resolved from the root `.venv`. Analysis targets Python 3.11 (the packages'
minimum) on all platforms.

`tests/typecheck/adapters.py` checks concrete adapter instances against their
port Protocols, including the ACP runner extensions and sandbox interfaces.
These assignments are checked statically without constructing adapters or
requiring credentials. Add a conformance assignment when adding an adapter or
port extension. Do not suppress diagnostics in these checks.

Existing diagnostics are baselined with line-level `pyright: ignore[rule]`
comments linked here. They cover existing payload narrowing, optional values,
third-party interfaces and stale workflow code; they do not disable any rule
across a file or directory. New code is checked normally. Fix and remove these
comments as the owning code is updated; do not expand the baseline for new code.
`reportUnnecessaryTypeIgnoreComment` makes obsolete suppressions fail the check.
The independent `langgraph-acp` distribution retains its existing mypy CI job.
