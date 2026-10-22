# OpenEngine V2

OpenEngine V2 is a grounds-up rewrite of OpenEngine in a service-oriented architecture. It's split into four services:
| Service Name | Shorthand | Description |
| -- | -- | --|
| Connection Bus | cxn | The way that external integrations communicate with OpenEngine. |
| Graph Engine | ge | The component that decides which steps need to execute and when. |
| Tool Service | tools | The tools agents may call, who may call them, and a durable record of every call. |
| Virtual Control Plane | vcp | The controller of which workspaces + agents are active. |

## Running locally
Requires [uv](https://docs.astral.sh/uv/). From `v2/`:

```bash
uv sync             # install the workspace
uv run oe up        # start the supervisor and NATS in the background (downloads nats-server on first run)
uv run oe migrate   # bring every service's database to head and create the NATS streams
uv run oe status    # what is running, and where (--json for scripts)
uv run oe logs -f   # follow the log, JSON lines shown in console form (--json for raw)
uv run oe down      # stop everything `oe up` started
```

State (the supervisor's log, NATS data and the SQLite databases) lives under
`$XDG_STATE_HOME/openengine`, `~/.local/state/openengine` by default. To see
logs and traces in Grafana, start a local OTLP stack first and point `oe up` at it:

```bash
docker run --rm -p 3000:3000 -p 4318:4318 grafana/otel-lgtm
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 uv run oe up
```

Run the tests with `uv run pytest`. See [apps/cli](apps/cli/README.md) for the
CLI and [packages/telemetry](packages/telemetry/README.md) for other telemetry
destinations.

The repo is structured as follows
```
apps/ -- the `oe` CLI and the supervisor `oe up` starts
docs/
  schema/ # each service's schema and ERD at every migration revision; latest_<service>.svg is the newest
infra/
  nats/
    nats-server.conf # nats server configuration
    streams/ # the durable JetStream streams `oe migrate` creates
    authentication.conf
integration_tests/ # tests which cross the NATS service boundary
packages/
  contracts/ # where the contracts between services live
  persistence/ # the database layer services share; one setting picks SQLite or Postgres
  telemetry/ # logging and tracing every process shares; one OTLP endpoint sets the destination
services/ # where the services themselves live
  connection_bus/
  graph_engine/
    ..
```

The services communicate over the NATS protocol and the [asyncapi 3](https://www.asyncapi.com/docs/reference/specification/v3.1.0) spec is used to describe the subjects and schemas.

All services contain a persistence directory which holds all of that service's database code. `OE_DATABASE_BACKEND` (`sqlite` by default, or `postgres`, a stub for now) picks the backend for every service; see [packages/persistence](packages/persistence/README.md). Each service's data is isolated: its own SQLite file, or its own Postgres schema.
```
    persistence/ # each service defines its own schema
      schema.py   # the service's tables
      store.py    # the Protocol the rest of the service depends on
      postgres/   # the store, written for Postgres
      sqlite/     # the store, written for SQLite
      migrations/ # one Alembic history that runs on both
```

| Service | Database | Tables |
| -- | -- | -- |
| connection_bus | `cxn` | connections |
| graph_engine | `ge` | graphs |
| tool_service | `tools` | tools, tool_grants, tool_invocations, tool_invocation_attempts |
| virtual_control_plane | `vcp` | workspaces |

Every process logs and traces through [packages/telemetry](packages/telemetry/README.md): JSON lines on stderr, which `oe logs` prints, and, with `OTEL_EXPORTER_OTLP_ENDPOINT` set, OTLP to any collector or backend. Trace context crosses NATS in `traceparent` headers.



TODO:
- [x] Write the CLI up command
- [x] Boot NATS on up 
- [x] Write the CLI migrate command
- [x] Migrate NATS on CLI migrate
- [x] Write shared postgres/sqlite migration layer
- [ ] Write shared NATS client
- [ ] Construct e2e testing environment
- [ ] Port graph engine
- [x] Global logging and tracing
