## CLI
`oe`, the terminal entry point for running OpenEngine V2 locally.

```
uv sync          # from v2/
uv run oe up     # start the supervisor in the background; returns once it is ready
uv run oe migrate # upgrade the services' databases, then create or update the NATS streams
uv run oe status # what is running, and where (--json for scripts)
uv run oe logs   # the supervisor's log, NATS included (-f to follow, --json for raw JSON lines)
uv run oe down   # stop everything `oe up` started
```

```
commands/   one module per command
config/     local paths and defaults
migrations/ what `oe migrate` applies: service databases, then NATS streams
output/     tables and JSON
```

State lives under `$XDG_STATE_HOME/openengine`: the supervisor's state file,
its log, NATS's JetStream data, and the services' SQLite databases
(`databases/`). `OE_DATABASE_BACKEND` switches every service's database; see
packages/persistence. The jobs, graphs, loops, workspaces and
connections commands arrive with the services behind them.
