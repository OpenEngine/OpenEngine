## Graph Engine
`ge`, which decides which steps of a graph run and when. Its NATS interface is
[`contracts/graph_engine/v1`](../../packages/contracts/graph_engine/v1/asyncapi.yaml).

### Serving
`python -m graph_engine` connects to the NATS at `OE_NATS_URL` and serves
`oe.v1.ge.query.>` and `oe.v1.ge.control.>` through
[`openengine-messaging`](../../packages/messaging/README.md). So far it
answers `graphs.apply`, `operations.{operation_id}.get` for the operations
it records, and `graphs.list`, still with an empty page. Every other subject
gets `UNSUPPORTED_CAPABILITY`.

```bash
uv run oe up
uv run oe graph apply FILE     # created NAME revision 1 (graph graph_…)
uv run oe graph list           # No graphs registered.
uv run oe graph list --json    # the query-response
```

### Graph registry
[`registry.py`](registry.py) serves `graphs.apply`. Each graph name has a
stable graph ID and immutable revisions numbered from 1, each keeping its
source, its format (`resource`) and the source's SHA-256. A Graph resource's
source is the canonical JSON of what was applied, so an apply whose
definition matches the latest revision is a no-op that returns that revision
(`created: false`), and a changed one adds the next. Python graphs get
`UNSUPPORTED_CAPABILITY`. `expected_version` is the latest revision: another
is `VERSION_CONFLICT`, and one for a name not yet applied is `NOT_FOUND`.

The acceptance, the revision, the operation and the command's receipt commit
in one transaction, and applies take turns, so concurrent applies number no
revision twice. A retry with the same command ID, subject, payload,
`expected_version` and causal path gets the original reply, including a
rejection; anything else with that ID is `COMMAND_ID_CONFLICT`. Receipts are
kept indefinitely so far, beyond the contract's 7 days.

GE brings its database to the head of its Alembic migrations as it starts,
so `oe up` alone is enough; `oe migrate` does the same for every service.

### Readiness
GE answers `GET /healthz` on `127.0.0.1` with 200 only while it is connected
to NATS and subscribed, and 503 otherwise. Once it is ready, it writes the
health URL to `OE_GE_READY_FILE`, if set, and logs `ready`.

| Variable | Default | |
| -- | -- | -- |
| `OE_NATS_URL` | `nats://127.0.0.1:4222` | set by the supervisor |
| `OE_GE_HEALTH_PORT` | `0` (any free port) | the health listener's port |
| `OE_GE_READY_FILE` | | where to write the health URL once ready; the supervisor sets it |

The supervisor waits for the ready file and then for `/healthz` to answer
200. Only then does it record `ge` as `ready` (its ADDRESS in `oe status` is
the health URL) and report itself READY. If GE exits first or is not ready
within 30 s, the supervisor writes FAILED with the reason, stops every
process and exits nonzero, so `oe up` fails too.

On SIGTERM GE logs `draining`, unsubscribes, answers the requests it has
already received, removes its ready file, logs `stopped` and exits 0.
A SIGTERM while GE is still retrying its first NATS connection ends the
retries, and GE exits 0 without having been ready.

### Local-operator mode
NATS under `oe up` listens only on loopback and has no accounts, so whoever
can reach it is the operator on that machine. GE authorizes each request
through the messaging package's `Authorizer` port. The default,
`LocalOperator`, allows a request when the NATS server listens on a loopback
address and rejects it with `PERMISSION_DENIED` otherwise. It reads that
address from the server's INFO, not from `OE_NATS_URL`: a server bound to
`0.0.0.0` is reachable from other hosts even when GE dials `127.0.0.1`, so
GE logs a warning at connect and refuses every request. Remote access
needs a different authorizer, which does not exist yet.
