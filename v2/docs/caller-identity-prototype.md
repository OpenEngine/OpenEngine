# Caller identity through shared messaging (#1051, first increment)

This is an opt-in transport prototype. It does not enable a secure managed profile
and does not complete #1051. Existing service startup retains its current behavior.

## Contract

A caller credential may publish only its own request namespace. The service derives
an immutable `Principal` from that namespace and passes it to the existing
`Authorizer`. Body and header fields do not establish identity. The application
resolves the resource's actual project and checks it with `authorize_project`.

| Boundary | Proposed wire interface | Enforcer |
| --- | --- | --- |
| Caller request | `oe.v1.caller.<principal>.<service>.<query or control>.<operation>` | NATS credential-specific publish permissions |
| Service ingress | Only the bound query/control subjects; no parallel legacy subscription | `Server.caller_ingress` |
| Application route | Existing `oe.v1.<service>.<kind>.<operation>` and unchanged envelope | Shared adapter unwraps once |
| Caller reply | `_INBOX.oe.<principal>.<unique suffix>` | Ingress checks before any decode, error or effect; NATS restricts subscriptions |
| Resource | Actual resource owner, including existing receipts | Application Authorizer and handler |

Connect the caller using its own NKey seed and
`inbox_prefix=reply_prefix(Principal("alice"))`; construct
`Client(connection, principal=Principal("alice"))`. Install
`CallerIngress(frozenset({...}))` on the server and supply an explicit resource
Authorizer. `LocalOperator` is rejected at startup in this profile.

These APIs do not verify the broker configuration. Their trust assumption is
mandatory: **every credential and ingress path must preserve the identity binding**.
An unrestricted credential or relay can impersonate callers. Service credentials
can publish replies and belong to the trusted base. In-process code is trusted;
`Server.handle(..., principal=...)` is an internal adapter/testing seam, not an
external authentication API. Broker endpoint verification remains #1221 / #1222.

The test fixture uses NKey challenge-response, separate caller/service credentials,
explicit per-user permissions and deny-all defaults. No private seed is written to
configuration or logs. No-grant permissions use `deny: [">"]`: an empty allow list
must not be treated as deny-all. These are the native mechanisms described in the
[NATS authentication](https://docs.nats.io/learn/security/authentication-basics) and
[authorization](https://docs.nats.io/learn/security/authorization) documentation.

## Admission and revocation

Replacing the server's `CallerIngress` snapshot withdraws a principal before its
next queued callback is admitted. Broker configuration reload removes its NKey and
disconnects existing sessions. The fixture waits for the disconnect, verifies a
new connection is refused, and verifies the other caller still works. Work that
entered a handler before the cutoff may finish. There is no claim of rollback,
atomic distributed policy propagation or production credential rotation.

## Evidence and limits

`integration_tests/test_caller_identity.py` runs real NATS with two callers and a
small synthetic resource service. It exercises successful calls, project and
receipt ownership checks, uncredentialed connections, namespace spoofing, legacy
subjects, events, administrative subjects, foreign inboxes, reply substitution,
empty grants, and revocation. A deliberately widened broker permission lets Alice
perform an effect attributed to Bob: the negative control exposes the exact trust
assumption rather than concealing it.

The fixture explicitly uses plaintext loopback to isolate caller authentication.
It does not qualify transport confidentiality or broker identity. Its resource and
receipt checks are specimen handlers, not GE implementation or durable attribution.

Remaining #1051 gates:

1. Review the wire namespace and integrate it with the merged #1222 trust interface.
2. Provision and persist managed caller/service credentials with qualified custody;
   produce least-privilege permissions for every service, event and admin route.
3. Wire the actual CLI, GE, VCP and runtime callers, including service-to-service
   delegation; verify real project/receipt ownership and durable attribution.
4. Implement acknowledged revocation, rotation, ordinary restart and recovery across
   broker and service policy snapshots. Qualify active and reconnecting sessions.
5. Make managed startup secure by default with an explicit, visible development
   profile. Run the full acceptance contract and independent review before closure.
