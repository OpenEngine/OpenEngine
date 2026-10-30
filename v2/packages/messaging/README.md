## Messaging
`openengine-messaging`, request/reply over NATS for the CLI and the services.
It follows the envelope rules in the
[contracts](../contracts/README.md#clients-and-servers) and reads the schemas
from `packages/contracts` through the `contracts` link beside its code.

```python
from openengine.messaging import Client, Route, Server

# A caller: each attempt waits `timeout`; a timeout resends the same bytes.
client = Client(nats_connection, timeout=2.0, attempts=3)
reply = await client.query("oe.v1.ge.query.graphs.list")
await client.command("oe.v1.ge.control.graphs.apply", payload, command_id=..., caused_by=parent_path)

# A service: subscribes to oe.v1.{service}.query.> and .control.>.
server = Server(nats_connection, "ge", nats_url, queries={"graphs.list": Route(handler, schema_reference)})
await server.start()
...
await server.close()   # drain and close the dedicated connection within a 5-second budget
```

| Module | |
| -- | -- |
| `envelopes` | builds commands and queries and checks bodies against contract schemas |
| `errors` | `ServiceError` for `error.schema.json`, with `retryable` kept as sent; `Unavailable` |
| `client` | the bounded, retrying requester |
| `server` | the subscription adapter, the `Authorizer` port and `LocalOperator` |

A route's key may name subject parameters, such as
`operations.{operation_id}.get`: the server checks each against
the type `common/v1/types.schema.json` defines under its name, answers a
malformed one with `INVALID_ARGUMENT` naming `subject.{name}`, and hands the
rest to the handler as `Request.parameters`.

A command route built with `Route(handler, receipts=True)` keeps receipts, so
the server checks only that a command's `command_id` is well formed and leaves
the rest of its envelope to the handler, which can then record rejecting it.

A handler whose resources belong to a project asks the `Authorizer` again,
through `authorize_project(request, project)`, with the project the request
names or the resource it reached belongs to. A project name in a payload only
says where to act; it grants nothing by itself, so an authorizer decides from
the actor's verified identity. `LocalOperator` allows every project to the
loopback operator.

The client raises `Unavailable` (`UNAVAILABLE`, retryable) when nothing
serves a subject or when no reply arrives after the last attempt, and
`ServiceError` for an error or rejected reply. The server answers every request
with a typed response, including malformed ones. It never puts an exception's
text in an `INTERNAL` reply.

With a live connection, authorization reads the current broker listening address
for each request, including after reconnect. Local-operator mode rejects an
unknown address or a broker listening beyond loopback. The constructor's
`listener` argument is only used for transport-free calls (`nats=None`).

`Server.close(timeout=5.0)` shares one budget between draining (at most four
fifths) and connection close. An outage or expired drain window proceeds to
close. If final close fails or exceeds the remaining budget, TCP transport is
aborted and the error propagates; the service exits non-zero. Normal outage
shutdown exits zero when close succeeds. This does not guarantee completion of
a handler's side effects or make interrupted operations safe to retry.

Services withdraw their ready file before draining and close their health
listener in cleanup. Their event loop cancels remaining tasks before VCP
releases its state-directory lock. Cancellation-resistant or blocking handlers
still require the supervisor's process-stop deadline; no Python timeout can
preempt them. Forced transport abort is covered for TCP, not WebSockets.

Local-operator mode assumes a trusted managed broker with no remote ingress.
Its listener check is not publisher identity; authenticated remote callers and
broker permissions are tracked in #1051.

Client settings reject booleans, nonnumeric or nonfinite timeouts, timeouts
outside `(0, 60]` seconds, and attempt counts outside the integer range
`1..10`. Defaults remain 2 seconds and 3 attempts. Only timeouts cause an
automatic retry; a service rejection or malformed reply is returned at once.
The encoded body is built once before attempting delivery, including generated
command ID, causal path, expected version and deadline.

Replies are checked against the common response envelope before callers use
them. Query replies must echo the generated or supplied request ID; command
replies must echo the command ID, including rejections. Missing or mismatched
identity, invalid JSON, invalid known field types, missing results or operation
IDs, and contradictory success/error fields raise nonretryable `ServiceError`
with code `INTERNAL`, the subject and `oe logs` guidance. Additive response
fields and unknown string error codes remain compatible. This checks the
common envelope; service-specific result validation belongs to the caller.
