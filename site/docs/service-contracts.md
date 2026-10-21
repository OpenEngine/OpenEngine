---
title: Service contracts
---

# Service contracts

OpenEngine V2 is three services that talk over [NATS](https://nats.io). Each
one's interface is a contract: the subjects it answers and publishes, and the
messages on them, written as an [AsyncAPI 3](https://www.asyncapi.com/docs/reference/specification/v3.1.0)
document with JSON Schemas.

| Service | Shorthand | What it does | Reference |
| -- | -- | -- | -- |
| Connection Bus | `cxn` | The way external integrations communicate with OpenEngine. | [Connection Bus contract](pathname:///contracts/connection_bus/v1/) |
| Graph Engine | `ge` | Decides which steps need to execute and when. | [Graph Engine contract](pathname:///contracts/graph_engine/v1/) |
| Virtual Control Plane | `vcp` | Controls which workspaces and agents are active. | [Virtual Control Plane contract](pathname:///contracts/virtual_control_plane/v1/) |

## Graph Engine resources

The documents you write for the Graph Engine, in Kubernetes style
(`apiVersion: openengine.cc/v2alpha1`, `kind`, `metadata`, `spec`):

| Kind | What it is | Schema |
| -- | -- | -- |
| [Graph](pathname:///contracts/graph_engine/v1/resources/#graph) | The steps of a piece of work and the flow between them. | [`graph.schema.yaml`](pathname:///contracts/graph_engine/v1/resources/graph.schema.yaml) |
| [Loop](pathname:///contracts/graph_engine/v1/resources/#loop) | Runs a graph on a schedule. | [`loop.schema.yaml`](pathname:///contracts/graph_engine/v1/resources/loop.schema.yaml) |
| [Tool](pathname:///contracts/graph_engine/v1/resources/#tool) | A tool agents can call from a graph's steps. | [`tool.schema.yaml`](pathname:///contracts/graph_engine/v1/resources/tool.schema.yaml) |

The [resource reference](pathname:///contracts/graph_engine/v1/resources/)
lists every field, with examples. It is generated from the same schemas that
validate resources, so the two cannot disagree.

These pages are generated from
[`v2/packages/contracts`](https://github.com/OpenEngine/OpenEngine/tree/main/v2/packages/contracts)
and republished whenever a contract changes.
