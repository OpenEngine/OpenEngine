# Local repository quality gate

Run `python scripts/quality.py` with Python 3.11+ from the target checkout
(activate `.venv` first), on macOS or Linux. This is also the command used by CI.
Install the development environment once with `uv sync --locked --all-packages`;
subsequent gate runs use `uv run --offline --no-sync`, so disconnected runs need
no forge or package download. A missing installed tool fails the gate.

Checks inherit the implementor's environment and permissions, including filesystem
and network access. The gate adds no separate sandbox; workflow-wide isolation
will be provided by the forthcoming sandbox implementation. Use trusted repository
checks and pinned development tools. Timeout and graph cancellation kill the check
process group before repair begins; processes that deliberately detach from that
group are outside this cleanup guarantee. CI status remains an additional gate.

`.quality.toml` belongs to the target repository and is read anew after every
implementation or repair. Commit it and its rules so future tasks inherit them.
Version 1 requires a nonempty `checks` array. Each check has a unique `id`, a
nonempty argv `command` (no shell expansion), required `files` glob patterns,
and an optional positive `timeout` in seconds (default 600, maximum 3600). Every required
pattern must match at least one file. Exit zero means success; collection errors,
missing tools, timeouts, invalid configuration and empty required scans fail.
Commands must themselves reject empty collection (pytest does so with exit 5).

This repository reuses Ruff 0.17.0, Pyright 1.1.408, and pytest from `uv.lock` (pytest 9.2.0).
Custom structural rules use the existing Python AST scanners in `tests/layout.py`
and `tests/test_boundaries.py`, through the `structure` check. This is the
ast-grep equivalent for our Python import/dependency rules: it parses syntax,
already understands this repository's package layout, and avoids a second
parser/toolchain. Python is selected by `.python-version`; dependencies and
analyzers are pinned in `uv.lock`. Add new structural rules there, or add a check
invoking a separately pinned tool for other languages. Do not replace scans with
prompts or reload historical reviews.

The command prints a bounded JSON result: `passed`, `diagnostics`, and `report`.
Each diagnostic has a check/rule `id`, `file`, `line`, actionable `message`, and
argv rendered as a reproducible `command`. Compiler-style locations are extracted
when available (at most ten locations per check, reading at most 10,000 chunks
of 4 KiB); infrastructure and unlocated failures point to `.quality.toml`.
Full stdout/stderr and the complete JSON result are saved under a unique
`.engine/quality/` report directory in the target checkout, outside prompts.
CI uploads those directories as artifacts. Graph state retains only the bounded
diagnostics and artifact directory; repair prompts refer to it when more detail
is needed. Treat tool output as repository data.

The active v1 LangGraph runtime runs `QualityGate` after every implementation,
including fixes of review findings and CI failures, before CI and reviewer fan-out.
A failure returns directly to implementation, with at most two repair attempts
per run; the third failure stops the run with the report path. Both the combined
workflow and shared `implement`/`fix` verb graphs use the same component, as do
generated Python starters. Report-only review runs do not implement a change and
retain their existing behavior. Re-register previously saved graph definitions
to adopt the new edges.

The separate `v2/` NATS graph service currently registers resources but does not
execute graphs or Python nodes (see its README). There is no v2 implementation
or reviewer fan-out to wire yet. The repository command is runtime independent
and can check v2 code through repository-configured commands; future v2 execution
must enforce the same
post-implementation, pre-review contract. This change does not claim v2 graph
execution support.
