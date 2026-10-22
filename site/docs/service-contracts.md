---
title: Service contracts
---

# Service contracts

OpenEngine V2 is four services that talk over [NATS](https://nats.io). Each
one's interface is a contract: the subjects it answers and publishes, and the
messages on them, written as an [AsyncAPI 3](https://www.asyncapi.com/docs/reference/specification/v3.1.0)
document with JSON Schemas.

| Service | Shorthand | What it does | Reference |
| -- | -- | -- | -- |
| Connection Bus | `cxn` | The way external integrations communicate with OpenEngine. | [Connection Bus contract](pathname:///contracts/connection_bus/v1/) |
| Graph Engine | `ge` | Decides which steps need to execute and when. | [Graph Engine contract](pathname:///contracts/graph_engine/v1/) |
| Tool Service | `tools` | Keeps the tools agents may call, who may call them, and a record of every call. | [Tool Service contract](pathname:///contracts/tool_service/v1/) |
| Virtual Control Plane | `vcp` | Controls which workspaces and agents are active. | [Virtual Control Plane contract](pathname:///contracts/virtual_control_plane/v1/) |

## Resources

The documents you write, in Kubernetes style (`apiVersion`, `kind`,
`metadata`, `spec`). You create and change one by applying it: a new name
creates it, a known one updates it.

| Kind | Service | What it is | Schema |
| -- | -- | -- | -- |
| [Connection](pathname:///contracts/connection_bus/v1/resources/#connection) | Connection Bus | A link to one account at an external provider. | [`connection.schema.yaml`](pathname:///contracts/connection_bus/v1/resources/connection.schema.yaml) |
| [Graph](pathname:///contracts/graph_engine/v1/resources/#graph) | Graph Engine | The steps of a piece of work and the flow between them. | [`graph.schema.yaml`](pathname:///contracts/graph_engine/v1/resources/graph.schema.yaml) |
| [Loop](pathname:///contracts/graph_engine/v1/resources/#loop) | Graph Engine | Runs a graph on a schedule. | [`loop.schema.yaml`](pathname:///contracts/graph_engine/v1/resources/loop.schema.yaml) |
| [Tool](pathname:///contracts/tool_service/v1/resources/#tool) | Tool Service | A capability agents can call, bound to one connection. | [`tool.schema.yaml`](pathname:///contracts/tool_service/v1/resources/tool.schema.yaml) |
| [ToolGrant](pathname:///contracts/tool_service/v1/resources/#toolgrant) | Tool Service | Lets a workspace, or one step in it, call a tool. Graphs declare them in each step's `tools`. | [`toolgrant.schema.yaml`](pathname:///contracts/tool_service/v1/resources/toolgrant.schema.yaml) |
| [ToolInvocation](pathname:///contracts/tool_service/v1/resources/#toolinvocation) | Tool Service | The record of one call. The service creates it; you read it. | [`toolinvocation.schema.yaml`](pathname:///contracts/tool_service/v1/resources/toolinvocation.schema.yaml) |
| [Workspace](pathname:///contracts/virtual_control_plane/v1/resources/#workspace) | Virtual Control Plane | A place where agents work, on a provider. | [`workspace.schema.yaml`](pathname:///contracts/virtual_control_plane/v1/resources/workspace.schema.yaml) |

Each service's resource reference
([Connection Bus](pathname:///contracts/connection_bus/v1/resources/),
[Graph Engine](pathname:///contracts/graph_engine/v1/resources/),
[Tool Service](pathname:///contracts/tool_service/v1/resources/),
[Virtual Control Plane](pathname:///contracts/virtual_control_plane/v1/resources/)) lists every
field, with examples. It is generated from the same schemas that validate
resources, so the two cannot disagree.

These pages are generated from
[`v2/packages/contracts`](https://github.com/OpenEngine/OpenEngine/tree/main/v2/packages/contracts)
and republished whenever a contract changes.
