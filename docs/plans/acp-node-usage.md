# Usage for every ACPNode

Status: proposed. This change delivers the specification only.

## Outcome and scope

Every ACPNode invocation tracks usage automatically. Callers can read tokens
and reported cost for each physical ACP session, including all its prompt
turns, steering, correction prompts, and approval continuations. Agents that
report no usage still have a usage record with unknown values. Unknown must
never appear as free execution or zero tokens.

Implement this once in `langgraph-acp`, then integrate both its standalone
`ACPNode` and Engine's graph-runtime `ACPNode`. Initial scope includes normalized
events, result access, durable Engine session accounting, and tests. Pricing
estimates, budgets/enforcement, billing reconciliation, dashboards, and
workflow/PR-wide aggregation are follow-up work.

## Existing code and gaps

- [ACPUsage and ACPResult](../../langgraph-acp/src/langgraph_acp/result.py)
  already define optional token, context, and USD cost fields. Every result has
  `usage`, but its description leaves turn versus session scope ambiguous.
- The [standalone node](../../langgraph-acp/src/langgraph_acp/node.py) collects
  text and stop reason and leaves usage empty. It creates a fresh session for
  each invocation.
- The [stdio transport](../../langgraph-acp/src/langgraph_acp/_stdio.py) maps
  `usage_update` to `ACPEventType.USAGE_UPDATED`, forwards the raw update, and
  exposes the prompt response as `PROMPT_COMPLETED`. Notifications outside an
  active prompt stream, including session-load replay, are currently dropped.
- The [Engine node](../../packages/graph_runtime_langgraph/src/engine/graph_runtime_langgraph/acp.py)
  can run multiple prompts in one session and load a continuation after an
  approval. Its event bridge handles tool activity, not usage; its output is a
  graph-state dictionary, not an `ACPResult`. Terminal-tool acceptance can
  cancel the prompt reader before the provider's final response.
- The [runtime store](../../packages/graph_runtime_langgraph/src/engine/graph_runtime_langgraph/store.py)
  persists events and the latest continuation for a logical session key. That
  replaceable continuation is insufficient as historical usage storage.

This specifies the Usage/Ticket 14 direction in the existing
[architecture plan](../langgraph-acp%20Architecture%20and%20Implementation%20Plan.md).
Provider payload support below is a proposed contract, not a claim that either
current adapter supplies all these measurements.

## Public contract

Retain `ACPUsage` as the shared immutable measurement value and preserve its
existing fields and JSON round-trip behavior. Define the following semantics:

| Field | Meaning and aggregation |
| --- | --- |
| `input_tokens`, `output_tokens` | Reported consumption for the declared scope; sum only disjoint turns. |
| `thought_tokens`, `cached_tokens` | Optional breakdowns; never add them to input/output to invent a total. Record the provider's inclusion semantics in provenance. |
| `context_used`, `context_size` | Latest context occupancy/capacity snapshots; never sum across updates or turns. |
| `cost_usd` | Explicitly reported USD cost only; no inferred price or currency conversion. |

Missing fields remain `None` and serialize as absent keys; explicit zero remains
zero. Reject negative counts, booleans, non-integral counts, and non-finite or
negative costs at normalization. Invalid telemetry produces a diagnostic and
leaves the last valid field intact, without failing the agent's work.

Add `ACPUsageSummary` with `turn` and `session` values of type `ACPUsage`, a
stable local `turn_id`, and per-field coverage (`unknown`, `partial`, or
`complete`). Complete means reported for the full declared scope, not verified
against a bill. Include source/provenance (adapter identity/version when known,
model when reported, source scope, and observation identifier) plus a turn
lifecycle status (`running`, `completed`, `cancelled`, `failed`, `interrupted`).
Coverage and lifecycle are independent: an interrupted turn may have valid
reported counts without a complete final total. Summaries are immutable,
JSON-serializable snapshots with a schema version.

Keep `ACPResult.usage` as **this prompt turn's** usage. Add an optional,
backward-compatible `usage_summary` field for the full summary; newly executed
nodes always populate it. Old serialized results without the field still load
and must not be interpreted as complete session totals. Standalone callers read
`result.usage_summary.session`; Engine callers read the same summary through
the runtime store and events. Do not add mutable `node.usage` state: the same
node definition can run concurrently.

Expose normalized summaries through `ACPEventType.USAGE_UPDATED` and a new
Engine `EventKind.USAGE_UPDATED` (`usage.updated`). The normalized ACP event's
data contains `schema_version` and `summary`; preserve an original payload under
`raw` where needed for compatibility. Engine payloads additionally identify
`session_key`, `agent`, and `session_id`; the existing event envelope supplies
run, node, and execution identity. Publish an initial unknown summary, accepted
changes, and a final lifecycle update even when no measurements arrived.

## Collection and accounting rules

Introduce a shared normalizer and accumulator in `langgraph-acp/usage.py`.
Both nodes feed it events; provider-specific mappings belong at the provider/
transport boundary, never in Engine's node. Give custom providers the same
normalized contract, with an unknown fallback requiring no configuration.

Each normalized observation declares its scope (`turn`, `session`, or
`context`), mode (`snapshot` or `delta`), identity, and only the fields it
reports. Never guess scope from a field name or numerical increase. Before
implementing a provider mapping, capture sanitized fixtures for the resolved
Codex and Claude ACP adapter versions and document the actual wire fields,
units, scope, replay behavior, and token-breakdown semantics. Unknown formats
remain raw diagnostics and unknown measurements. Context occupancy alone is
not evidence of consumed input tokens.

1. Create a session accumulator when `session/new` or `session/load` succeeds,
   and allocate a turn ID before every `session.prompt` call. For Engine, restore
   the persisted session summary before loading a continuation. Capture usage
   during load at the transport boundary without replaying historical text into
   the new prompt. Establish a baseline before deriving any turn difference.
2. Merge snapshots field by field, replacing reported values within their
   declared scope. Repeated snapshots do not add cost. Apply deltas once using
   stable observation IDs; do not support replayable deltas without an identity
   guarantee. Reject stale ordered observations. A decreasing cumulative value
   without a documented reset/correction is a diagnostic and marks coverage
   partial; do not silently subtract previously recorded consumption.
3. A session-scoped cumulative field is authoritative for that field; do not
   add turn totals to it. Without such a snapshot, derive a session field from
   disjoint tracked turns. A missing measurement on any contributing turn makes
   the derived sum partial; retain the known subtotal, with coverage attached.
   With no reported contributions, the field remains unknown.
4. A session-total difference may supply a turn field only with trustworthy
   before/after baselines, matching counter semantics, and exclusive ownership
   of the session during that interval. Otherwise the turn field is unknown.
   Session totals from a previously existing session can include historical work;
   they are session lifetime totals, not necessarily this run's attributable cost.
5. Parse supported usage from both streaming updates and prompt completion.
   Reconcile both into the same scoped counters: a final snapshot supersedes
   provisional data rather than being added again. One missing field must not
   erase another reported field. Keep decimal arithmetic internally for cost
   aggregation and convert only at the existing float `cost_usd` boundary.
   Non-USD reported cost stays in provenance with its currency and leaves
   `cost_usd` unknown.
6. Finalize in cleanup paths as well as success paths. Preserve observed usage
   on cancellation, provider failure, approval suspension, and terminal-tool
   races. Do not delay accepted completion indefinitely to obtain telemetry;
   use existing cancellation/drain behavior and mark incomplete fields partial.
   A standalone failure keeps its exception behavior; attach the observed
   summary to contextual ACP errors and retain emitted events for cancellation.
   An abrupt process loss may lose unobserved/unpersisted usage; never claim
   exactly-once billing or fabricate a final total.

For example, two identical session snapshots reporting 100 input tokens and
$0.01 followed by a snapshot of 160 tokens and $0.016 produce a session total
of 160 and $0.016. The second turn is 60 and $0.006 only when the baseline and
ownership conditions above hold. A later update containing only context
occupancy changes neither consumption nor cost.

## Engine durability and access

Use `(run_id, agent, session_id)` as the session accounting key within Engine;
retain `session_key`, node ID, and execution IDs as attribution. The same
physical session reused within a run shares one accumulator. A replacement
session gets its own record even when its logical key is unchanged. Preserve
earlier records when continuation bindings are replaced or forgotten. Shared
sessions across runs are not deduplicated into a global spend total in v1.

Extend `GraphRuntimeStore` and its memory/SQLite implementations with atomic
usage recording and `session_usage(run_id, agent, session_id)` /
`usage_sessions(run_id)` reads. Store a versioned session summary, turn records,
counter baselines, and accepted observation identities/order sufficient to
resume the reducer. Persist each accepted observation's reducer state and its
runtime event in one transaction, then notify subscribers. Serialize concurrent
writes per session (or use revision checks) to prevent lost updates. A replayed
observation must neither advance totals nor publish a duplicate accounting event.

The usage ledger is independent of LangGraph state/checkpoint rollback. A graph
retry that actually prompts the agent again creates another turn and counts its
real work; replaying saved events does not. A recovered unfinished turn is marked
interrupted until a trustworthy provider snapshot reconciles it. Restoring a
checkpoint must not erase spend from an abandoned attempt.

Generate an Alembic revision for the new usage tables under
`migrations/sqlite_graph` using `alembic_config(database_url, store="graph")`
and `alembic.command.revision`; apply with
`engine-migrate --store graph <database_url>`. Do not add runtime DDL or modify
LangGraph-owned checkpoint schemas. Existing sessions without ledger data read
as unknown; do not backfill zero usage. Existing event subscribers gain the new
event kind, while graph output keys and terminal output behavior remain intact.

## Delivery and acceptance

1. **Shared contract and reducer:** extend result serialization, normalized
   events, provider fixtures, and accumulator tests in `langgraph-acp/tests`.
   Verify absent versus zero, invalid fields, mixed scope, partial coverage,
   duplicate/stale observations, final reconciliation, currency handling, and
   context snapshots. Verify old result payloads still deserialize.
2. **Both node integrations:** extend the fake ACP agent and node/transport tests
   to prove automatic usage for standalone and custom providers, missing usage,
   completion-only usage, load replay, and concurrent independent sessions.
   Every successful new result has a summary even without provider support.
3. **Engine durability:** implement ledger/store methods, the graph migration,
   and runtime events. Extend `tests/test_graph_runtime_langgraph_acp.py` and
   store/migration tests for multiple steering/correction turns, approval resume
   after restart, replacement sessions, event replay, checkpoint retry,
   cancellation, failures, and accepted terminal results racing final usage.
   Verify memory/SQLite parity and atomic deduplication after restart.
4. **Compatibility and documentation:** update the public API examples and
   adapter compatibility coverage. Record supported fields and unsupported gaps
   for each tested adapter version. Test a fixture provider with full counts and
   cost as well as a real adapter that lacks a field; adapter incompleteness
   must remain visible rather than blocking all ACPNode use.

Acceptance is a queryable per-session summary with trustworthy scope and
coverage for every Engine ACPNode session, equivalent results/events from the
standalone node, and no double counting on resume or replay. Exact tokens or
cost cannot be promised when the provider does not supply them. If fixture
inspection finds either built-in adapter lacks consumption or cost, document
that gap and scope upstream adapter support separately; do not substitute
context size or pricing estimates to make the fields appear complete.
