## Supervisor
The background process `oe up` starts. It starts OpenEngine's local processes,
waits for each to be ready, records what it is doing in
`$XDG_STATE_HOME/openengine/supervisor.json`, and stops them all, last started
first, on SIGTERM. If a process exits, the supervisor stops the rest and exits
too.

VCP starts on every `oe up` and direct `oe-supervisor` invocation, alongside
cxn, ge, and tools; no opt-in flag is required. It starts last and stops first.

```
processes/  how each process is fetched and started (NATS today)
readiness/  how the supervisor knows a process is ready
shutdown/   signals in, children stopped
state.py    the state file `oe` reads
```

The nats-server binary is not bundled. The first start downloads the pinned
release for this platform, checks it against the checksum in
`processes/nats.py`, and caches it under `$XDG_CACHE_HOME/openengine`. To bump
NATS, change `NATS_VERSION` and replace `ARCHIVE_SHA256` with the matching lines
from that release's `SHA256SUMS`.
