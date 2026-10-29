# Managed broker authentication

The local deployment started by `oe up` verifies its NATS broker before starting services. A competing broker cannot satisfy that check by answering HTTP health requests or supplying a readiness file. The supervisor requires verified TLS and a NATS protocol response; GE and VCP then pass their existing service readiness checks. Broker authentication does not prove functional VCP readiness or identify a child by PID.

## Identity and connections

Each supervisor launch creates a fresh CA and one broker certificate. The CA signing key is not persisted. The broker key stays in the private launch directory; `broker.json` contains only public trust metadata. Clients verify both the launch CA and the certificate's unique hostname, using native NATS TLS-first before protocol messages or credentials.

The supervisor probe, GE, VCP, graph CLI and managed stream migrations use the shared connector. GE and VCP load trust once; reconnects retain that trust. Missing or invalid managed trust fails closed. Status URLs cannot select another endpoint. CXN and tools do not yet open broker connections in their current entrypoints.

The generated certificates expire after 30 days. Expired certificates are rejected on new connections and reconnects. Existing TLS connections are not terminated merely because the certificate expires. Long-running deployments need a planned restart before expiry; a new launch creates new trust. Automatic certificate rotation is not implemented.

## Filesystem requirements and upgrades

The default state directory is `$XDG_STATE_HOME/openengine` when `XDG_STATE_HOME` is absolute; otherwise it is `$HOME/.local/state/openengine`.

Managed state requires a canonical absolute path with no symlink components. The state and launch directories must belong to the operating user with mode `0700`; protected files require private permissions (`0600` when created). Ancestors must not permit untrusted namespace replacement. Checks use open descriptors and reject unsafe file types, symlinks and extended ACL grants. macOS deny-only ancestor ACLs remain permitted. Linux access/default ACLs are rejected; unsupported ACL inspection fails closed.

An older installation may have less restrictive permissions. Stop it before upgrading, inspect ownership and ACLs, and explicitly correct the owned state directory, logs and protected state files. Choose a canonical private location if the existing path is symlinked or shared. Startup does not automatically repair permissions, remove ACLs or adopt an unsafe directory. Do not blindly change permissions recursively on shared storage.

JetStream data remains outside the disposable launch directory. Normal shutdown stops owned children before retiring keys and the manifest. If child cleanup times out, launch material is preserved for the surviving child; later launches never adopt that trust. Abandoned generations may need operator cleanup after confirming their children have stopped.

## Security boundary

The supervisor, service and CLI code, NATS executable, dependencies, launch configuration, trust records and OS must be trusted. Root and processes sharing the operator's UID belong to that trusted domain: they can read private keys and modify code or configuration. Mode bits, ACL checks and TLS do not isolate malicious same-user code. Untrusted agents require a separate OS-enforced boundary that also prevents process-memory access and key/control-state access. Docker is not required for that boundary.

Server authentication does not authenticate callers. LocalOperator still trusts clients able to reach the loopback broker; another local client can establish TLS without proving an operator identity. NKeys/JWT, client admission and subject permissions remain [#1051](https://github.com/OpenEngine/OpenEngine/issues/1051). This change does not make a shared-user or multi-user host safe for untrusted workloads.

An explicit `oe migrate --nats-url nats://…` selects an external plaintext broker and does not inherit managed trust. Standalone services without `OE_NATS_TRUST_FILE` likewise retain external plaintext mode. The shared connector requires explicit `BrokerTrust` for `tls://`; external TLS configuration through the migration CLI is not supported in this slice.

Regression tests cover identity rejection, handshake failure and protected filesystem metadata. They are evidence for these specific checks, not a formal security proof or a test of every operating-system isolation configuration.

## Implementation

- [Launch identity and custody](../apps/supervisor/src/openengine/supervisor/broker_launch.py)
- [Broker readiness](../apps/supervisor/src/openengine/supervisor/readiness/broker.py)
- [Connection and filesystem checks](../packages/messaging/src/openengine/messaging/broker.py)
- [LocalOperator authorization](../packages/messaging/src/openengine/messaging/server.py)
- [Security enhancement #1221](https://github.com/OpenEngine/OpenEngine/issues/1221)
