## Graph Engine
`ge`, which decides which steps of a graph run and when. Its NATS interface is
[`contracts/graph_engine/v1`](../../packages/contracts/graph_engine/v1/asyncapi.yaml).

### Serving
`python -m graph_engine` connects to the NATS at `OE_NATS_URL` and serves
`oe.v1.ge.query.>` and `oe.v1.ge.control.>` through
[`openengine-messaging`](../../packages/messaging/README.md). So far it
answers one query, `graphs.list`, with an empty page until the graph registry
exists. Every other subject gets `UNSUPPORTED_CAPABILITY`.

```bash
uv run oe up
uv run oe graph list           # No graphs registered.
uv run oe graph list --json    # the query-response
```

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

### Local-operator mode
NATS under `oe up` listens only on loopback and has no accounts, so whoever
can reach it is the operator on that machine. GE authorizes each request
through the messaging package's `Authorizer` port. The default,
`LocalOperator`, allows a request when GE's NATS listener is a loopback
address and rejects it with `PERMISSION_DENIED` otherwise. Remote access
needs a different authorizer, which does not exist yet.
