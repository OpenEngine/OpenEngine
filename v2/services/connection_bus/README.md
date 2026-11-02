# Conneciton Bus - CXN

The CXN is for resolving incoming webhook events into a new or existing Job.
## Persistence and webhook ingress

Normal access uses `PERSISTENCE.open(config).scoped(verified_tenant)`; callers
must derive that UUID from verified credentials. Every connection read and
write filters on that tenant. Creating a connection claims its globally unique
`(provider, external_id)` route in the same transaction; deleting it releases
the route. A collision raises `IntegrityError` and rolls back both writes.

There are no public CXN handlers yet. Ingress adapters should authenticate the
provider webhook, then call `persistence.routing.resolve_webhook(provider,
external_id, config)`. This boundary reads only `connection_routes` globally,
validates its non-nil tenant UUID, and finds the connection within that tenant.
Unknown or stale routes return `None`, with no fallback to a global connection
search. Webhook payloads cannot supply a tenant. The CXN Alembic revision
backfills existing connections and routes to `LOCAL_TENANT`.
