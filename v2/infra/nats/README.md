# NATS
`nats-server.conf` is the configuration the supervisor starts nats-server with.
The supervisor package links to it, so edits here take effect on the next
`oe up`. The supervisor supplies the listen address, ports and JetStream store
directory on the command line.
