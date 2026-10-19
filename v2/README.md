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
infra/
  nats/
    nats-server.conf # nats server configuration
    authentication.conf
integration_tests/ # tests which cross the NATS service boundary
packages/
  contracts/ # where the contracts between services live
services/ # where the services themselves live
  connection_bus/
  graph_engine/
    ..
```

The services communicate over the NATS protocol and the [asyncapi 3](https://www.asyncapi.com/docs/reference/specification/v3.1.0) spec is used to describe the subjects and schemas.

All services contain a persistence directory which contains the migrations necessary to produce the database layer for that service.
```
    persistence/ # each service defines its own schema
      postgres/
      sqlite/
      migrations/
```



TODO:
- [x] Write the CLI up command
- [x] Boot NATS on up 
- [ ] Write the CLI migrate command
- [ ] Migrate NATS on CLI migrate
- [ ] Write shared postgres/sqlite migration layer
- [ ] Write shared NATS client
- [ ] Construct e2e testing environment
- [ ] Port graph engine
- [ ] Global logging/monitoring
