## CLI
`oe`, the terminal entry point for running OpenEngine V2 locally.

```
uv sync          # from v2/
uv run oe up     # start the supervisor in the background; returns once it is ready
uv run oe migrate # upgrade the services' databases, then create or update the NATS streams
uv run oe status # what is running, and where (--json for scripts)
uv run oe logs   # the supervisor's log, NATS included (-f to follow, --json for raw JSON lines)
uv run oe graph list # the graphs GE has registered, over NATS (--json for the query-response)
uv run oe graph apply FILE # register each Graph in FILE with GE (--json for each response and operation)
uv run oe graph get NAME[@REVISION] # a graph's revision, the latest by default, and its source (--json)
uv run oe down   # stop everything `oe up` started

uv run oe graph validate FILE  # check a Graph file (- reads standard input)
uv run oe graph spec           # the Graph resource reference and expression syntax, as Markdown
```

`oe status` shows each process's own status: `ready` once it passed its
readiness check (NATS and GE), `running` for a service that has none yet.
When OpenEngine cannot answer (no supervisor, NATS unreachable, or no reply
from GE), `oe graph list`, `get` and `apply` say so on stderr, point to
`oe up` and `oe logs`, and exit 1.

Graph names are unique per project. `oe graph list`, `get` and `apply` act in
one project: `--project NAME`, else `$OE_PROJECT`, else `default`, which holds
every graph applied before projects existed. A project name selects graphs; it
grants no access by itself (under `oe up` the local operator may use every
project).

`oe graph list` shows every graph in the project, following `next_cursor`
through each page: its project, name, graph ID, latest revision and when it
was last updated. `--json`
prints one page's query-response, `next_cursor` included; pass that to
`--cursor` for the next page.

`oe graph get NAME[@REVISION]` finds NAME's graph ID through `graphs.list`
in the project alone (its `name_prefix`, then the exact name) and reads the revision from
`graphs.{graph_id}.get`. It prints the revision, its format, its source's
SHA-256 digest and when it was created, then the source: a Graph resource as
YAML, from the canonical JSON GE keeps. `--json` prints the query-response.
An unknown name or revision prints `oe graph get: NOT_FOUND: …` on stderr and
exits 1.

`oe graph apply FILE` checks the file as `validate` does, sends each Graph to
GE's `graphs.apply`, reads the operation it recorded and prints one line per
Graph: `created NAME revision 1 in project P (graph ID)` for a name new to the
project, `updated NAME revision N in project P (graph ID)` when the definition
changed, or `unchanged NAME revision N in project P (graph ID)` when it matches
the latest revision. The graph ID never changes; the same name in another
project is another graph. A command that gets no reply in time is resent with the same command
ID, so a lost reply never adds a revision. GE rejects a Python graph with
`UNSUPPORTED_CAPABILITY`, and `apply` sends only Graph resources.

`oe graph validate` and `oe graph spec` work offline: they read the Graph
schema from openengine-contracts and need no `oe up`, NATS or runner
credentials. `validate` prints each problem as `FILE: PATH: message`, such as
`-: spec.plan[0]: set exactly one of agent, human, ci` or
`bad.graph.yaml: spec.flow[2].to: unknown step 'nowhere'`, and exits 1. It
reports every independent problem at once: the schema's, and the graph's own
rules (unique and unreserved step names, `flow` naming steps that exist,
every step reachable from `start` and reaching `end`, cycles passing through
a `route`, one route per step, and the syntax and names of every expression).
A valid file prints `FILE: valid Graph NAME` for each Graph and a note that
runners and tools are checked when a run starts, since an offline check
cannot know them, and exits 0. Any other kind, such as a Loop, and V1
manifests (`apiVersion: openengine.cc/v1`) are refused. `oe graph spec` ends
with the expression syntax: roots, operators, functions and examples.

```
commands/   one module per command
config/     local paths and defaults
migrations/ what `oe migrate` applies: service databases, then NATS streams
output/     tables and JSON
```

State lives under `$XDG_STATE_HOME/openengine`: the supervisor's state file,
its log, NATS's JetStream data, and the services' SQLite databases
(`databases/`). `OE_DATABASE_BACKEND` switches every service's database; see
packages/persistence. The jobs, loops, workspaces and connections commands,
and the rest of the graph commands, arrive with the services behind them.
