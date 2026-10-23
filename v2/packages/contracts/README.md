# OpenEngine V2 service contracts

The NATS interfaces of the four V2 services, as AsyncAPI documents and
JSON Schemas:

| Service | Shorthand | Document | Reference |
| -- | -- | -- | -- |
| Connection Bus | `cxn` | [`connection_bus/v1/asyncapi.yaml`](connection_bus/v1/asyncapi.yaml) | [`connection_bus/v1/reference.html`](connection_bus/v1/reference.html) ([view](https://openengine.cc/contracts/connection_bus/v1/)) |
| Graph Engine | `ge` | [`graph_engine/v1/asyncapi.yaml`](graph_engine/v1/asyncapi.yaml) | [`graph_engine/v1/reference.html`](graph_engine/v1/reference.html) ([view](https://openengine.cc/contracts/graph_engine/v1/)) |
| Tool Service | `tools` | [`tool_service/v1/asyncapi.yaml`](tool_service/v1/asyncapi.yaml) | [`tool_service/v1/reference.html`](tool_service/v1/reference.html) ([view](https://openengine.cc/contracts/tool_service/v1/)) |
| Virtual Control Plane | `vcp` | [`virtual_control_plane/v1/asyncapi.yaml`](virtual_control_plane/v1/asyncapi.yaml) | [`virtual_control_plane/v1/reference.html`](virtual_control_plane/v1/reference.html) ([view](https://openengine.cc/contracts/virtual_control_plane/v1/)) |

Each document is written from its service's point of view: the requests it
answers (`action: receive`, with a typed reply) and the events it publishes
(`action: send`).

## Implementation status

**These are contracts, not implementations.** As of this release:

| Defined here | Implemented |
| -- | -- |
| Control and query subjects (`oe.v1.…`) with typed requests and replies | No service answers them. No handlers exist. |
| Event subjects and payloads (`oe.v1.{svc}.events.…`, `oe.v1.events.toolinvocations.…`) | The `cxn-events`, `ge-events`, `tools-events` and `vcp-events` streams exist (`oe migrate`) and capture these subjects. Nothing publishes to them yet. |
| Operation, job, execution, workspace, session and approval models | No persistence or state machine implements them. |
| Tool, grant and invocation tables (`tools` database) | Migrations only; no store reads or writes them yet. |
| Validation tooling | Implemented: schema, subject, cross-field and example checks; AsyncAPI validation and HTML rendering. |

This work creates no consumers or broker permissions. The tool service adds
one stream, `tools-events`, and declares one queue group, `tool-workers`, for
the execution workers on `oe.v1.tools.invoke.*.*`. The other event streams
capture `oe.v1.{svc}.events.>`, so events share the `oe.v1` namespace. Workspace-daemon contracts and
provider ingress/egress payloads (`cxn.ingress.>`, `cxn.egress.>`) are out of
scope.

## Layout

```text
common/v1/                 envelopes and types every service shares
  types.schema.json          identifiers, causal paths, timestamps, versions
  command.schema.json        command-response.schema.json
  query.schema.json          query-response.schema.json
  event.schema.json          error.schema.json
  pagination.schema.json     operation.schema.json
  meta.schema.yaml           metadata every resource shares
{service}/v1/
  asyncapi.yaml            the service's channels, operations and messages
  schemas/                 typed payloads, grouped with $defs
  examples/                one JSON file per example message
  resources/               the resources people write, where the service has any
tests/                     pytest suite run with the rest of v2
tooling/                   pinned AsyncAPI parser, validate.py and reference.py
```

Schemas are JSON Schema draft-07 (the newest draft the AsyncAPI parser
accepts), referenced from AsyncAPI as
`application/schema+json;version=draft-07`. Service schemas compose the
common envelopes with `allOf` and never copy them.

## Resources

The documents people write are Kubernetes-style resources: `apiVersion`,
`kind`, `metadata` and `spec` (and `status`, where the service reports one),
camelCase fields, lists of named objects, and unions written as exactly one of
several keys. They are created and changed by **applying** them: a new name
creates the resource, a known one updates it.

Every kind, whichever service owns it, has `apiVersion: openengine.cc/v1alpha1`
(see [Versions](#versions)).

| Service | Kinds | Applied with |
| -- | -- | -- |
| Connection Bus | `Connection` | `connections.apply` |
| Graph Engine | `Graph`, `Loop` | `graphs.apply`, `loops.apply` |
| Tool Service | `Tool`, `ToolGrant`, `ToolInvocation` | `tools.apply`; `toolgrants.apply` by GE from a Graph step's `agent.tools`; invocations are created by `tools.invoke` |
| Virtual Control Plane | `Workspace` | `workspaces.apply` |

Each kind is defined once, as a JSON Schema in YAML under
`{service}/v1/resources/`, sharing `common/v1/meta.schema.yaml`, with examples
in `resources/examples/`. Those files are the single source of truth:

- the apply commands' payloads `$ref` them, so the contract validates the
  resource a command carries;
- `tooling/resources.py` validates resource files against them
  (`validate_resource`, `validate_text`), reporting every problem with its
  path, such as `spec.plan[0].agent.prompt`;
- the same script generates each service's `resources/reference.html`
  ([Connection Bus](https://openengine.cc/contracts/connection_bus/v1/resources/),
  [Graph Engine](https://openengine.cc/contracts/graph_engine/v1/resources/),
  [Tool Service](https://openengine.cc/contracts/tool_service/v1/resources/),
  [Virtual Control Plane](https://openengine.cc/contracts/virtual_control_plane/v1/resources/)),
  a static page of every kind, type, field and example. A test fails when one
  is stale.

The schemas check structure. Rules that span fields or resources, such as
unique step names, `flow` naming steps that exist, a Tool's connection
existing or an adapter supporting a `recovery`, are the owning service's to
check when a resource is applied.

## Versions

Every service is on one version line, `v1`. It appears as the subject
namespace, the directory and the resource apiVersion, and those three always
change together:

| Identifier | Where | What it means | Changes when |
| -- | -- | -- | -- |
| Subject namespace | `v1` in `oe.v1.…` | The wire API generation | a breaking change; `oe.v2` is served alongside `oe.v1` until callers move |
| Contract directory | `{service}/v1/`, `common/v1/` | The files that define that generation | with the subject namespace: `oe.v2` lives in `{service}/v2/` |
| Resource apiVersion | `apiVersion: openengine.cc/v1alpha1` on every kind | One API group, `openengine.cc`, for every service's resources, at the subject namespace's version plus a stability suffix | the `v1` part with the subject namespace; the suffix as `v1` matures: `v1alpha1`, `v1alpha2`, …, `v1beta1`, then `v1` once stable |
| Contract release | `info.version` in each `asyncapi.yaml` (semver; now `1.0.0-draft.2`) | One release of a service's document | any contract change: patch for wording, minor for compatible additions, major for breaking changes. While a draft, every change bumps `draft.N` instead |
| AsyncAPI format | `asyncapi: 3.1.0` in each document | The specification the documents are written in | the documents move to a newer AsyncAPI specification |

The resource apiVersion is defined once, as `api_version` in
[`meta.schema.yaml`](common/v1/meta.schema.yaml); every resource schema refers
to it, and a test fails if a schema, example or message names another.

"V2", as in OpenEngine V2 and the `v2/` source directory (and the
`urn:openengine:v2:…` document IDs), is the name of this generation of the
codebase. It is not an API version and never appears in a subject, directory
or apiVersion. V1 OpenEngine's graph files (`apiVersion: openengine.cc/v1`)
are a separate format.

## Subjects

| Kind | Subject | Transport |
| -- | -- | -- |
| Control | `oe.v1.{svc}.control.{resource}[.{id}].{action}` | NATS request/reply |
| Query | `oe.v1.{svc}.query.{resource}[.{id}].{view}` | NATS request/reply |
| Event | `oe.v1.{svc}.events.{partition}.{id}.{event_type}` | JetStream publish to the `{svc}-events` stream |

The tool service follows its own subject layout, which separates resource
management from execution: `oe.v1.tools.{apply,get,list,delete}`,
`oe.v1.tools.invoke.{namespace}.{name}`, `oe.v1.toolgrants.…`,
`oe.v1.toolinvocations.…`, and events on
`oe.v1.events.toolinvocations.{namespace}.{invocation_id}.{event}` in the
`tools-events` stream. Its namespaces and names are subject tokens of
lower-case letters, digits and `-` (`resource_name` in `types.schema.json`).

Replies use NATS's native reply subject; no payload carries `replyTo`.

Each `{svc}-events` stream captures `oe.v1.{svc}.events.>`. Control and
query subjects are captured by no stream, so request/reply traffic is never
persisted; the tests assert both.

Every `{…_id}` in a subject is a single subject token (letters, digits, `_`,
`-`; no `.`, `*`, `>` or spaces) with its type's prefix (`conn_`, `job_`,
`ws_`, …), defined in [`types.schema.json`](common/v1/types.schema.json). A
service answers a malformed token with `INVALID_ARGUMENT`. When an ID appears
in both the subject and the body, they must agree. An event's `event_type`
ends with the tokens its subject has after the last ID: the whole name
(`….{job_id}.job.created`) or its fact (`….{invocation_id}.blocked-auth`).

## Messages

### Commands

[`command.schema.json`](common/v1/command.schema.json):
`command_id`, `causal_path`, optional `expected_version` and `deadline_at`,
and the operation's typed `payload`.

- **Accepted means durably recorded, not completed.** An `accepted` reply
  carries the `operation_id` of the service's durable operation; completion
  arrives as a lifecycle event carrying `{operation_id, command_id}`, and in GE
  and VCP through `operations.{operation_id}.get`. CXN exposes no operation
  query.
- **Idempotency.** Retrying with the same `command_id` and identical subject,
  payload, `expected_version` and `causal_path` returns the original reply and
  operation ID. Reusing a `command_id` with anything else is rejected with
  `COMMAND_ID_CONFLICT`. Command IDs are retained for at least **7 days**,
  matching the event streams' retention; after that a reused ID may be
  treated as new. Non-retryable rejections are retained and replayed;
  retryable rejections (`UNAVAILABLE`) are not, so a retry may succeed.
- **Timeouts.** A request that times out has an unknown outcome; it does not
  mean rejection. Retry with the same `command_id`.
- **Deadlines.** `deadline_at` bounds acceptance only. A command not accepted
  by then is rejected with `DEADLINE_EXCEEDED`; accepted work is never
  cancelled by its deadline. A retry of a command that was already accepted
  returns the acceptance even after its deadline.
- **Identity.** The acting identity is taken from the verified NATS
  authentication or a verified delegation, never from a payload field.
  Envelopes are closed, so an `actor` field is rejected.

### Causal paths

`causal_path` replaces correlation and causation IDs: an array of IDs, root
first, ending with the ID of the message that carries it. Display it joined
with `.` if useful; transmit the array.

- A root command or event has `[self_id]`.
- Each downstream command or event appends its own ID to the path of its
  primary cause. Retries and republication keep the same ID and path.
- Parallel work produces sibling paths: the same prefix, different last
  elements.
- Fan-in keeps one primary path (the dependency that completed the join, or
  the first one recorded). The other dependencies are kept in GE's execution
  records, not in the path.
- Events may appear in paths.
- An external root is scoped by provider and connection so two providers can
  never produce the same ID: `{provider}-{connection_id}-{external_id}`, for
  example `gh-conn_acme_gh-dlv_4411`. An external ID that is too long or not
  token-safe is replaced by a base32 SHA-256 digest of it.
- **Limit: 32 elements, unique.** A message whose path would exceed 32
  elements is not sent; the operation that would have sent it fails with
  `CAUSAL_PATH_TOO_LONG`. A command whose path is already too long is
  rejected with the same code. Paths are never truncated.

### Queries

[`query.schema.json`](common/v1/query.schema.json): optional `request_id`
(echoed, for diagnostics only) and required `params` (`{}` when there are
none). Replies have `status: ok` with `result`, or `status: error` with
`error`. Queries create no operation.

### Pagination

[`pagination.schema.json`](common/v1/pagination.schema.json): requests send
`limit` (default 50, maximum 200) and an opaque `cursor`; results return
`items` and `next_cursor` (`null` on the last page).

- Each list documents its sort order, always ending in the resource ID.
- A cursor is bound to the filters it was issued with; changing them is
  `INVALID_CURSOR`. Cursors stay valid for at least 24 hours.
- Pagination is by key, not offset: records that change between pages are
  neither repeated nor skipped unless their sort key or filter membership
  changed; records created after the first page appear only if they sort
  after the cursor.

### Events

[`event.schema.json`](common/v1/event.schema.json): `event_id`,
`event_type`, `occurred_at`, `producer`, `causal_path` (ending with
`event_id`), typed `resources`, `resource_version` where the partition
resource is versioned, and a typed `payload`.

- Events may be delivered more than once. Deduplicate on `event_id`. It is
  also sent as the `Nats-Msg-Id` header, so the stream drops republication
  within its 2-minute duplicate window.
- `occurred_at` does not order events globally. Order is guaranteed only
  among events of one partition (the resource in the subject), in publish
  order.
- Event families and their exact subjects are in each service's document.
  Beyond the families the work order lists, the contracts add:
  - CXN: `connection.recovered`, the counterpart of `connection.degraded`.
  - GE: `execution.resumed`, and `job.completed` / `job.failed` /
    `job.cancelled`. `job.cancelled` is the only event that means a job is
    permanently stopped.
  A cancelled graph execution does not by itself mean the job is permanently
  cancelled; workspace failure, task failure and operation failure are
  separate events with separate meanings.

### Trace context

Every command, query and event may carry the W3C Trace Context headers
`traceparent` and `tracestate`
([`types.schema.json#/$defs/trace_headers`](common/v1/types.schema.json)), through
the `commandHeaders`, `queryHeaders` and `eventHeaders` message traits. A
receiver continues the sender's trace from them, and starts a new one when a
message carries none. Replies carry none; they belong to the requester's span.
[`openengine-telemetry`](../telemetry/README.md) writes and reads them.

### Errors

[`error.schema.json`](common/v1/error.schema.json): `code`, `message`,
`retryable`, and typed `details` per code.

| Code | Meaning | Retryable |
| -- | -- | -- |
| `INVALID_ARGUMENT` | Malformed message or subject token, or subject/body disagreement | no |
| `NOT_FOUND` | A named resource does not exist or is not visible | per response |
| `PERMISSION_DENIED` | The verified actor may not do this | no |
| `INVALID_STATE` | The resource's state does not allow this | per response |
| `VERSION_CONFLICT` | `expected_version` did not match | no |
| `COMMAND_ID_CONFLICT` | Command ID reused with different contents, operation or target | no |
| `UNSUPPORTED_CAPABILITY` | The service or provider cannot do this | no |
| `DEADLINE_EXCEEDED` | `deadline_at` passed before acceptance | no |
| `UNAVAILABLE` | Temporarily unable; retry with the same command ID | yes |
| `CAUSAL_PATH_TOO_LONG` | The 32-element path limit would be exceeded | no |
| `STALE_APPROVAL` | Approval decision targets a superseded, expired, decided or older-generation request | no |
| `INVALID_CURSOR` | Cursor malformed, expired, or issued for other filters | no |
| `PROVIDER_ERROR` | An external provider failed (operations and lifecycle failures) | per response |
| `INTERNAL` | Unexpected service fault | per response |

### Operations

[`operation.schema.json`](common/v1/operation.schema.json): `operation_id`,
`command_id`, `operation` (the command, such as `jobs.cancel`, which
discriminates the typed `result`), `status` (`pending`, `running`,
`succeeded`, `failed`, `cancelled`), `resources`, timestamps, and `result`
on success or `error` on failure. Operation status is not job, task or
workspace status: a succeeded `jobs.cancel` operation means cancellation was
carried out.

## Compatibility

- **Requests are strict.** Every schema is closed
  (`additionalProperties: false`). A service rejects an unknown request field
  with `INVALID_ARGUMENT`, so a misspelt option fails loudly instead of being
  ignored. A client must not send a field before the service's release
  defines it.
- **Readers are tolerant.** Minor releases may add optional fields to replies,
  snapshots and event payloads, and new event types. Consumers must ignore
  unknown fields and event types, and must not reject a reply or event
  because it validates only against a newer release's schema.
- **Enums.** Minor releases may add values to enums in replies and events
  (states, error codes, reasons). Consumers must treat an unknown value as
  "other": an unknown error code by its `retryable` flag, an unknown state as
  not terminal. Request enums gain values only together with the capability
  that uses them.
- **Never in a minor release:** removing or renaming a field, making an
  optional request field required, narrowing a type or pattern, changing a
  subject, or changing the meaning of an existing value. Those need a new
  subject namespace (`v2`).
- **Defaults** are part of the contract; changing one is breaking.

## Tooling

The [Pages workflow](../../../.github/workflows/pages.yml) regenerates and
checks the references against the checked-in pages, then publishes them to
`https://openengine.cc/contracts/{service}/v1/` on every contract change to main,
and each resource reference to `https://openengine.cc/contracts/{service}/v1/resources/`.
Every schema file is published at its own path too, so a schema's relative
`$ref`s resolve against the site, as in
`https://openengine.cc/contracts/graph_engine/v1/resources/graph.schema.yaml`.

Everything is pinned: the AsyncAPI parser in
[`tooling/package.json`](tooling/package.json) and its lockfile, the Python
validator's `jsonschema` and `pyyaml` in `v2/uv.lock`, and the AsyncAPI React
component the reference pages load, by exact version in `reference.py`.

```bash
cd v2/packages/contracts/tooling
npm ci                      # install the pinned AsyncAPI parser
npm run validate            # parse each document and resolve every $ref it reaches

cd v2
uv run python packages/contracts/tooling/validate.py              # examples, subjects, cross-field rules
uv run python packages/contracts/tooling/reference.py             # regenerate {service}/v1/reference.html
uv run python packages/contracts/tooling/reference.py --bundle    # also write dist/{service}/asyncapi.json
uv run python packages/contracts/tooling/resources.py            # regenerate {service}/v1/resources/reference.html
uv run python packages/contracts/tooling/resources.py check F..  # validate resource files
uv run pytest packages/contracts/tests                            # all of the above, plus malformed variants
```

To preview a contract locally, regenerate its reference and open
`{service}/v1/reference.html` in a browser. The page works from disk; it
loads the renderer from jsDelivr.

- **`validate.mjs`** runs the AsyncAPI parser, which checks each document
  against the AsyncAPI 3.1 specification and resolves every schema and example
  reference.
- **`validate.py`** covers what the parser and JSON Schema cannot: every
  example validates against its message's schema, subject templates and token
  types, subject/body ID agreement, causal paths ending in the message's own
  ID, event subjects matching `event_type`, `Nats-Msg-Id` equal to
  `event_id`, replies matching their request's operation, and an example for
  every message. Services can call `validate.validate_message` on live
  traffic in their own tests.
- **`reference.py`** resolves every file reference into one self-contained
  document (written by `--bundle`) and embeds it in each service's
  `reference.html`, rendered in the browser by the AsyncAPI React component.
  A test fails when a checked-in page is out of date, so regenerate it after
  changing a contract.
