OpenEngine is split into three services:
| Service Name | Shorthand | Description |
| -- | -- | --|
| Connection Bus | cxn | The way that external integrations communicate with OpenEngine. |
| Graph Engine | ge | The component that decides which steps need to execute and when. |
| Virtual Control Plane | vcp | The controller of which workspaces + agents are active. |

They communicate via NATS and the contracts between each service are stored in packages/contracts. 

## Database schemas
Each service owns its database (see [packages/persistence](packages/persistence/README.md)). These ERDs show each service's latest schema and are regenerated, with each migration revision's own ERD and SQL, under [docs/schema](docs/schema) whenever a migration changes. Highlights mark what the latest revision added, removed or changed.

| Service | Latest ERD |
| -- | -- |
| Connection Bus (cxn) | [latest_cxn.svg](docs/schema/latest_cxn.svg) |
| Graph Engine (ge) | [latest_ge.svg](docs/schema/latest_ge.svg) |
| Virtual Control Plane (vcp) | [latest_vcp.svg](docs/schema/latest_vcp.svg) |
