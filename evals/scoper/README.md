# Scoper evaluations

This suite keeps model quality checks separate from parser and domain contract
tests. `cases.json` is versioned and each case belongs to `train`, `dev`, or the
held-out `test` split. Never pass the test split to an optimizer.

Normal CI runs the deterministic validators:

```sh
uv run pytest tests/test_scoper_evals.py
```

DSPy optimization is an explicit, credentialed operation because it calls live
models and writes `compiled.json` for review:

```sh
uv sync --group scoper-evals
uv run --group scoper-evals python evals/scoper/optimize.py
```

Configure DSPy's language model before running the second command. Review the
compiled artifact and compare it against the held-out cases before changing the
production prompt. The optimizer gives malformed JSON a zero; production-domain
structural validation remains the hard gate before any judge score is considered.

The current domain schema assigns IDs only after work orders are created. For that
reason, dependencies in a proposed spec can reference existing work orders but not
other specs in the same plan. `dangling_dependency` failures protect this boundary.
