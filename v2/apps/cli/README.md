## CLI
`oe`, the terminal entry point for running OpenEngine V2 locally.

```
uv sync          # from v2/
uv run oe up     # start the supervisor in the background; returns once it is ready
uv run oe migrate # upgrade the services' databases, then create or update the NATS streams
uv run oe status # what is running, and where (--json for scripts)
uv run oe logs   # the supervisor's log, NATS included (-f to follow, --json for raw JSON lines)
uv run oe graph list # the graphs GE has registered, over NATS (--json for the query-response)
uv run oe down   # stop everything `oe up` started
```

`oe status` shows each process's own status: `ready` once it passed its
readiness check (NATS and GE), `running` for a service that has none yet.
When OpenEngine cannot answer (no supervisor, NATS unreachable, or no reply
from GE), `oe graph list` says so on stderr, points to `oe up` and `oe logs`,
and exits 1.

```
commands/   one module per command
config/     local paths and defaults
migrations/ what `oe migrate` applies: service databases, then NATS streams
output/     tables and JSON
```

State lives under `$XDG_STATE_HOME/openengine`: the supervisor's state file,
its log, NATS's JetStream data, and the services' SQLite databases
(`databases/`). `OE_DATABASE_BACKEND` switches every service's database; see
packages/persistence. The jobs, loops, workspaces and
connections commands, and the rest of the graph commands, arrive with the services behind them.
