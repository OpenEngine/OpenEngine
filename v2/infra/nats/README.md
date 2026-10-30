# NATS
`nats-server.conf` is the configuration the supervisor starts nats-server with.
The supervisor package links to it, so edits here take effect on the next
`oe up`. The supervisor supplies the listen address, ports and JetStream store
directory on the command line.

`streams/` holds the durable JetStream streams, one file per stream named after
it, in the JSON the JetStream API takes (`nats stream add --config` reads the
same shape). `oe migrate` creates the missing streams on the running server and
updates those whose config has drifted; fields a file leaves out take the
server's defaults. It never deletes a stream, so removing a file leaves the
stream in place.

| Stream | Subjects | Retention |
| -- | -- | -- |
| cxn-ingress | `cxn.ingress.>` | workqueue |
| cxn-egress | `cxn.egress.>` | workqueue |
| cxn-events | `oe.v1.cxn.events.>` | limits |
| ge-events | `oe.v1.ge.events.>` | limits |
| vcp-events | `oe.v1.vcp.events.>` | limits |
| tools-events | `oe.v1.tools.events.>` | limits |

All keep messages for 7 days on file storage, up to 1 GiB per stream, and
deduplicate publishes that repeat a `Nats-Msg-Id` within 2 minutes. A full
stream rejects new publishes (`discard: new`) rather than evict messages that
have not been handled yet.

## Operator mode

`oe up` runs the single-account server above, so every service is dedicated
to `LOCAL_TENANT` and nothing here changes for it. The shared deployment runs
nats-server in [operator mode](https://docs.nats.io/running-a-nats-service/configuration/securing_nats/auth_intro/jwt)
instead, provisioned by `openengine.messaging.operator` in
[packages/messaging](../../packages/messaging/README.md#operator-mode):

```bash
uv run python -m openengine.messaging.operator provision DIRECTORY
nats-server --config DIRECTORY/nats-server.conf --addr 127.0.0.1 --port 4222 --http_port 8222 --store_dir STORE
```

`provision` writes to `DIRECTORY`:

| File | |
| -- | -- |
| `operator.jwt`, `operator.nk` | the operator, self-signed, naming the system account; `.nk` holds its seed |
| `SYS.jwt`, `SYS.nk` | the system account, signed by the operator |
| `service.jwt`, `service.nk` | the service account every shared service connects to |
| `{cxn,ge,tools,vcp}.creds` | one user per shared service, issued by the service account |
| `nats-server.conf` | operator mode with `system_account` and a memory resolver preloading every account JWT |

Like the local config, it leaves the listen address, ports and JetStream store
directory to the command line. Seeds and `.creds` are secrets, written
owner-only.

The service account exports, for `svc` in `cxn`, `ge`, `tools` and `vcp`,
`oe.v1.*.{svc}.control.>` and `oe.v1.*.{svc}.query.>` as services and
`oe.v1.*.{svc}.events.>` as a stream, every one with
`account_token_position: 3`. A tenant account imports them with its own key
as the third token, `oe.v1.{its key}.{svc}.…`, mapped to the tenant-facing
`oe.v1.{svc}.…` (the contracts'
[Tenants](../../packages/contracts/README.md#tenants) section): NATS refuses
an import whose third token is any other account's key, so a shared service
sees the caller's account key as token 3 and never a forged one. The webhook
subjects `cxn.ingress.>` and `cxn.egress.>` stay inside the service account
and are never exported. The live tests in `packages/messaging/tests` boot
this server with a test-only tenant and prove the mapping; provisioning real
tenant accounts is #1237.
