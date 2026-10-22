# Workspace daemon

Implementation is pending. The [internal runtime protocol](../../packages/contracts/internal/workspace_runtime/v1/README.md)
defines execute/cancel/inspect messages, durable acknowledgements, runtime updates,
identity checks and recovery. It is separate from VCP's public client API.

The daemon runs inside a provisioned workspace VM, owns its local attempt journal
and executor processes, and keeps command reception responsive while agents run.
The next implementation step will introduce durable sessions/attempts in VCP,
followed by this daemon with a fake executor. Steer, approvals, checkpoint and
suspend are outside this execution milestone.
