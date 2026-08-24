# Hardcoded workflow removal acceptance suite

This directory contains the regression assets and acceptance gates for deleting
`packages/engine/src/engine/core/workflows/implementation_review.py`.

The suite is intentionally separate from the general workflow unit tests:

- `test_golden_event_trace.py` replays the repository-owned workflow through
  the generic interpreter and compares it with behavior captured from commit
  `d15456f`, before the workflow DSL existed. The trace normalizes legacy
  implementation/review phases to `running_agent` and clears stale active-run
  IDs in terminal states so it compares workflow behavior rather than obsolete
  runtime representation.
- `test_legacy_sqlite_rejection.py` opens a database written by the SQLite
  adapter at `d15456f` and verifies the explicit policy that pre-DSL active runs
  are unsupported rather than migrated through workflow-specific code.
- `test_removal_gate.py` uses Python's AST to prove that the legacy module and
  all imports of it are gone.

All tests are active acceptance checks. No compatibility path or expected
failure remains after the hardcoded reducer is removed.

Run only this clearly labeled suite with:

```bash
uv run pytest -m workflow_removal_acceptance -rxX
```

## Fixture provenance

`fixtures/pre_workflow_definitions.sqlite3` and
`fixtures/implementation_review_trace.json` are produced by
`generate_legacy_fixtures.py` while the imports resolve against commit
`d15456f`. They are committed test inputs, so the normal test run does not need
Git, a temporary checkout, or fixture generation.

To regenerate them deliberately:

```bash
legacy_tree=$(mktemp -d)
git archive d15456f | tar -x -C "$legacy_tree"
legacy_pythonpath=$(find "$legacy_tree/packages" "$legacy_tree/apps" \
  -type d -name src -print | paste -sd: -)
PYTHONPATH="$legacy_pythonpath" python \
  tests/workflow_removal_acceptance/generate_legacy_fixtures.py \
  --output tests/workflow_removal_acceptance/fixtures
```

The generator refuses to overwrite existing assets without `--force`.
