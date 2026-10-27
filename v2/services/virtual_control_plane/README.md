# Virtual Control Plane - VCP
The lifecycle manager of Agents, and the Workspaces they're using to get their work done.

Data Model
Workspace | A place in which an agent is doing work.
WorkspaceProvider | A virtual machine substrate providing the Workspaces | process vs smolvm & kubernetes
Approval | A request for approval awaiting decision.

## Service skeleton (this slice)

VCP is opt-in: the supervisor starts it only when `oe up --enable-vcp` (or
`oe-supervisor --enable-vcp`) asks for it, after every other service and NATS;
it is stopped first, and stopping it never destroys a workspace. It connects
to NATS, serves its subjects through the shared `openengine.messaging`
`Server`, and answers `GET /healthz` only once it actually holds its state
directory's exclusive lock, is connected and is subscribed -- the same
truthful-readiness contract `ge` uses. Authorization runs through the shared
`Authorizer` boundary before any handler, defaulting to `LocalOperator`
(loopback only, judged by where the broker listens -- not the URL VCP
dialed).

Only `capabilities.get` is served, and it always reports no providers: this
slice registers none. Workspace commands (`workspaces.apply`, `.execute`,
...) are out of scope here.

### Where the ports go

`application/ports.py` defines the seams later tickets implement against:

- `WorkspaceProviderPort` (create / recover / destroy an opaque-locator
  workspace) -- implemented by #1054's Git worktree/host-process adapter and
  #1114's smolvm adapter. `application/workspaces.py`'s `WorkspaceService`
  drives both through this one port, and
  `tests/test_provider_conformance.py` runs the same lifecycle assertions
  against a worktree-shaped and a VM-shaped double, so neither real adapter
  needs its own copy of this test.
- `RuntimeSupervisorPort` and `ExecutionContext` -- starting and fencing the
  agent process/session inside an already-provisioned workspace, kept apart
  from provisioning so a worktree never needs a VM to exist. #1055 (host
  fencing) and #1118 (durable fake workspace daemon delivery) implement it.
- `UnitOfWork` and `EventPublisher` -- placeholders for the transactional
  command receipts and event outbox #1111 and #1112 add; nothing here
  implements them yet.

Provider selection is sticky and persisted: `workspaces.locator` (added by
the `add workspace locator` migration) holds whatever opaque
ownership/placement metadata the provider that created a workspace needs to
reassert ownership of it, including after this process restarts. Recovering
with an unregistered or unavailable provider raises rather than silently
reprovisioning with a different one or a lower-isolation fallback --
`tests/test_workspaces_service.py` covers both failures.

### Out of scope here

Real worktree/smolvm adapters, VM allocation, agent execution, workspace
commands, and authenticated public (non-loopback) callers (#1051, which plugs
into the same `Authorizer` boundary once it exists).
