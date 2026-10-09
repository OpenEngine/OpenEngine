# Python lint baseline

Run `uv run ruff check` from the repository root. The dependency-boundaries job
in `.github/workflows/tests.yml` runs the same command after a locked workspace
install. Ruff is pinned in the root dev group and configuration so an analyzer
upgrade is an explicit baseline review.

The root configuration covers Python source, tests, migrations, scripts and
workflows, including `langgraph-acp`. Package pyprojects without a Ruff section
inherit it. The independent `apps/open-verify` distribution keeps its existing
Ruff configuration; the root command also checks it with those rules.

The baseline enables `E4`, `E7`, `E9`, `F`, `C901`, `PLR0912`, `PLR0913`,
`PLR0915`, `BLE001` and all stable `TRY` rules. Initial measured maxima with
Ruff 0.16.10 are:

| Rule | Ceiling | Current owner | Refactor |
| --- | ---: | --- | --- |
| C901 | 534 | `web.api.create_app` | #748 |
| PLR0912 | 35 | `TerminalMcpBroker._repository_call` | #755 |
| PLR0913 | 33 | `web.api.create_app` | #743 |
| PLR0915 | 1459 | `web.api.create_app` | #748 |

[Issue #779](https://github.com/OpenEngine/OpenEngine/issues/779) tracks removal
of every exact-file/rule exception in `pyproject.toml`, including the existing
undefined workflow names in `web.api`. New files receive the full rule set.
File-level exceptions still allow that particular violation in the named file;
remove them as the corresponding code is refactored. Do not expand the list or
raise ceilings to accommodate new code.

Broad exception boundaries use individual `BLE001` suppressions with a reporting
contract and follow-up issue, so adding another broad handler in the same file
is checked. These are HTTP/MCP/JSON-RPC responses, task outcomes and health
reports; unexpected graph HTTP errors are re-raised by `_refusal`. The two
previously silent reply/teardown boundaries now log their failures. The 15
`BaseException` handlers outside Open Verify clean up or report failure and then
re-raise, preserving cancellation; they need no suppression.

When retiring debt, inspect suppressed diagnostics with:

```sh
uv run ruff check --ignore-noqa --config 'lint.per-file-ignores = {}' --statistics
```

Lower the complexity ceilings as the owner refactors land, remove resolved
file entries and inline suppressions, and run the normal root check again.
