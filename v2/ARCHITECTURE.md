# OpenEngine V2 architecture

## Security architecture

[Open the interactive security atlas](https://openengine.cc/v2/security/). The map separates code on its pinned main revision from contracts, ticket requirements, unmerged PRs and proposals, with source anchors and captured evidence for each area. Check its snapshot date before treating a ticket or implementation status as current.

## Services
OpenEngine is split into four services:

| Service Name | Shorthand | Description | Contract |
| -- | -- | -- | -- |
| Connection Bus | cxn | The way that external integrations communicate with OpenEngine. | [cxn contract](https://openengine.cc/contracts/connection_bus/v1/) |
| Graph Engine | ge | The component that decides which steps need to execute and when. | [ge contract](https://openengine.cc/contracts/graph_engine/v1/) |
| Tool Service | tools | The tools agents may call, who may call them, and a durable record of every call. | [tools contract](https://openengine.cc/contracts/tool_service/v1/) |
| Virtual Control Plane | vcp | The controller of which workspaces + agents are active. | [vcp contract](https://openengine.cc/contracts/virtual_control_plane/v1/) |

They communicate via NATS and the contracts between each service are stored in [packages/contracts](packages/contracts/README.md). The references are republished to the site whenever a contract changes.

## Resources
The documents people write are Kubernetes-style resources (`apiVersion`, `kind`, `metadata`, `spec`, and `status` where the service reports one). They are created and changed by **applying** them: a new name creates the resource, a known one updates it. Each service owns its kinds; each kind is a JSON Schema in YAML under `packages/contracts/{service}/v1/resources/`, which both validates resources and generates the service's resource reference. Every kind shares one API group and version, `openengine.cc/v1alpha1`, whose `v1` matches the `oe.v1` subjects; see [Versions](packages/contracts/README.md#versions).

### Connection Bus (cxn) · [reference](https://openengine.cc/contracts/connection_bus/v1/resources/)
| Kind | apiVersion | What it is | Applied with |
| -- | -- | -- | -- |
| [Connection](https://openengine.cc/contracts/connection_bus/v1/resources/#connection) | `openengine.cc/v1alpha1` | A link to one account at an external provider, with a credential reference and a scope. Tools act through one. | `oe.v1.cxn.control.connections.apply` |

### Graph Engine (ge) · [reference](https://openengine.cc/contracts/graph_engine/v1/resources/)
| Kind | apiVersion | What it is | Applied with |
| -- | -- | -- | -- |
| [Graph](https://openengine.cc/contracts/graph_engine/v1/resources/#graph) | `openengine.cc/v1alpha1` | The steps of a piece of work and the flow between them. Each change is an immutable revision. | `oe.v1.ge.control.graphs.apply` |
| [Loop](https://openengine.cc/contracts/graph_engine/v1/resources/#loop) | `openengine.cc/v1alpha1` | Runs a graph on a schedule, creating a job each time. | `oe.v1.ge.control.loops.apply` |

### Tool Service (tools) · [reference](https://openengine.cc/contracts/tool_service/v1/resources/)
| Kind | apiVersion | What it is | Applied with |
| -- | -- | -- | -- |
| [Tool](https://openengine.cc/contracts/tool_service/v1/resources/#tool) | `openengine.cc/v1alpha1` | An invocable capability bound to one Connection, with its adapter, input and output schemas and recovery behaviour. | `oe.v1.tools.apply` |
 | [ToolGrant](https://openengine.cc/contracts/tool_service/v1/resources/#toolgrant) | `openengine.cc/v1alpha1` | Lets a workspace, or one step attempt in it, call one tool, under a Cedar `when` condition and until it expires. Graphs declare them inline: a step's `agent.tools` entries, each a `namespace.name` or `{tool, when}`, are its grants. | `oe.v1.tools.grants.apply`, by GE once a step attempt has its workspace and before it runs; deleted when the attempt ends |
 | [Policy](https://openengine.cc/contracts/tool_service/v1/resources/#policy) | `openengine.cc/v1alpha1` | A Cedar `forbid` an operator applies as a ceiling over grants: a call is allowed only if a grant permits it and no policy forbids it. Trusted callers only. | `oe.v1.tools.policies.apply` |
 | [ToolInvocation](https://openengine.cc/contracts/tool_service/v1/resources/#toolinvocation) | `openengine.cc/v1alpha1` | The durable record of one call, with the tool definition it resolved. Created by the service, never applied. | created by `oe.v1.tools.invoke.{namespace}.{name}` |

### Virtual Control Plane (vcp) · [reference](https://openengine.cc/contracts/virtual_control_plane/v1/resources/)
| Kind | apiVersion | What it is | Applied with |
| -- | -- | -- | -- |
| [Workspace](https://openengine.cc/contracts/virtual_control_plane/v1/resources/#workspace) | `openengine.cc/v1alpha1` | A stable place where agents work, on a provider. GE names it before it runs anything, so tool grants can refer to it. | `oe.v1.vcp.control.workspaces.apply`, by GE |

Because GE applies a step's tool grants before each step that lists tools, and a call is authorized in Cedar (a grant's `permit` plus the operator's `forbid` policies) when it is accepted and again before every dispatch, the tool service is on the critical path of graph execution and must be highly available.

## Architecture diagram
Each service with its NATS subjects (control, query and events) and its tables, the apps and integrations that connect to them, and how VCP runs workspaces. It is drawn from the contracts, the migrations and `infra/nats/streams` by [`architecture.py`](packages/contracts/tooling/architecture.py).

![OpenEngine V2 architecture](docs/architecture.svg)

Open the image in a new tab if it appears too small to read. 
## Database schemas
Each service owns its database (see [packages/persistence](packages/persistence/README.md)). Tables are declared tenant-scoped (the existing ones move over in #1227-#1229): `tenant_id` leads its key, uniques and foreign keys, and stores are only reached for one tenant at a time; the few global tables are listed in [global_tables.toml](packages/persistence/src/openengine/persistence/tenancy/global_tables.toml). See [Tenancy](packages/persistence/README.md#tenancy). These ERDs show each service's latest schema and are regenerated, with each migration revision's own ERD and SQL, under [docs/schema](docs/schema) whenever a migration changes. Highlights mark what the latest revision added, removed or changed.

| Service | Latest ERD |
| -- | -- |
| Connection Bus (cxn) | [latest_cxn.svg](docs/schema/latest_cxn.svg) |
| Graph Engine (ge) | [latest_ge.svg](docs/schema/latest_ge.svg) |
| Tool Service (tools) | [latest_tools.svg](docs/schema/latest_tools.svg) |
| Virtual Control Plane (vcp) | [latest_vcp.svg](docs/schema/latest_vcp.svg) |

## Public request tenancy

GE and VCP load trusted process configuration using `Deployment.from_environment`.
`OE_TENANCY_MODE=dedicated` uses `OE_TENANT_ID` (default LOCAL_TENANT).
`shared` resolves broker-written account tokens using a cached resolver over
the `OE_ACCOUNT_TENANTS` JSON account-to-tenant mapping. Unknown accounts are
refused; payload identity grants no access.

Local authorization retains LocalOperator's broker loopback restriction. Shared
mode requires explicit `OE_AUTHORIZATION=credentialed` and an
`OE_NATS_CREDENTIALS` file for the service account. Credentialed authorization
trusts broker account imports and permissions and allows all projects within
the verified tenant. Operators must restrict account-scoped subjects to trusted
services; this is not user-level project RBAC. Dedicated deployments can also
explicitly select credentialed authorization with tenant credentials.

Handlers receive `Request.principal`. `Deployment.public_client(connection, request)`
binds outbound public calls to that principal and the same mode. GE has no
public downstream calls yet; VCP's `oe.internal.v1` runtime transport remains
separate. VCP storage and event streams are outside this tenancy change.
The CLI uses `OE_NATS_CREDENTIALS` with unchanged plain subjects. Supervisor-managed
`oe up` overrides inherited tenancy, authorization and credentials to dedicated
LOCAL_TENANT on its single-account loopback broker, requiring no operator setup.
