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
