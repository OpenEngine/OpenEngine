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
await server.drain()   # stop taking requests, answer the ones received
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

The client raises `Unavailable` (`UNAVAILABLE`, retryable) when nothing
serves a subject or when no reply arrives after the last attempt, and
`ServiceError` for an error or rejected reply. The server answers every request
with a typed response, including malformed ones. It never puts an exception's
text in an `INTERNAL` reply.
