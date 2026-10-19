# OpenEngine V2

OpenEngine V2 is a grounds-up rewrite of OpenEngine in a service-oriented architecture. It's split into three services:
| Service Name | Shorthand | Description |
| -- | -- | --|
| Connection Bus | cxn | The way that external integrations communicate with OpenEngine. |
| Graph Engine | ge | The component that decides which steps need to execute and when. |
| Virtual Control Plane | vcp | The controller of which workspaces + agents are active. |

It is essentially a durable way to execute and pace long-running work. 

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
| virtual_control_plane | `vcp` | workspaces |



TODO:
- [x] Write the CLI up command
- [x] Boot NATS on up 
- [x] Write the CLI migrate command
- [x] Migrate NATS on CLI migrate
- [x] Write shared postgres/sqlite migration layer
- [ ] Write shared NATS client
- [ ] Construct e2e testing environment
- [ ] Port graph engine
- [ ] Global logging/monitoring
