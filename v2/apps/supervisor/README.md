## Supervisor
The background process `oe up` starts. It starts OpenEngine's local processes,
waits for each to be ready, records what it is doing in
`$XDG_STATE_HOME/openengine/supervisor.json`, and stops them all, last started
first, on SIGTERM. If a process exits, the supervisor stops the rest and exits
too.

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

## Virtual Control Plane

`oe up` always starts VCP after every other service; set `OE_VCP_SMOLVM_IMAGE`
first to register the smolvm provider, or leave it unset to start with no
providers configured. VCP readiness is recorded in supervisor status. Shutdown
retains workspace VMs; explicit VCP destroy commands remove them. See
[the VCP service guide](../../services/virtual_control_plane/README.md) for the
local-account trust boundary, persistence and provider prerequisites.
