# Review findings ledger

The state-store Alembic history creates `review_findings` and `finding_outcomes`.
Run `engine-migrate sqlite:///path/to/state.sqlite3` when upgrading a deployment.
The graph/checkpoint databases do not own these tables.

Raw and reranked terminal outputs retain reviewer lineage. Triage records a
selected/rejected outcome on every reranked finding, and selected findings also
get a `triaged` row. Identities hash run, stage, file, line, and tagline; retries
reuse that identity. Outcomes are unique per finding and signal.

The reranker passes the structured `finding` argument to `add_comment`. Its
successful comment callback records the posted finding immediately, before
terminal completion, including the returned GitHub ID. General comments retain
the finding's original location. The CLI posts through GitHub's JSON API and
sends comment metadata to the service's run-scoped ledger endpoint. The server
uses the persisted reranked finding's lineage, rather than trusting client copies.

Merged pull-request webhooks enqueue reconciliation through the existing ingress
worker, including bot merges. Bot merges still cannot approve work orders.
Reconciliation reads full GitHub blobs at the recorded head and merge commits
and compares changes in the reviewed file's coordinates within ±3 lines. It
records `fixed_before_merge` or `unchanged_at_merge`, with the merge SHA as the
value. These are proximity signals, not an authenticity judgment. A deleted
file counts as changed; unavailable revisions, truncated trees, and decoding
failures are logged without manufacturing an outcome. Findings without a head
SHA or location cannot be reconciled.

All capture is best effort: ledger errors are logged and do not alter review,
posting, triage, or work-order approval. Runtimes without an injected ledger do
not record findings. The SQLite deployment injects the state store's ledger;
other deployments can implement the optional `FindingsLedger` port. The existing
ingress queue is process-local; failed or interrupted observations can be retried
through the `engine.runtime.findings.reconcile` function. There is no labeling
job or dashboard in this phase.
