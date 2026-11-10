"""Ingress boundary: resolve only the global route before accessing tenant data."""

from __future__ import annotations

from openengine.persistence import DatabaseConfig
from openengine.persistence.tenancy import tenant

from connection_bus.persistence import PERSISTENCE
from connection_bus.persistence.schema import connection_routes
from connection_bus.persistence.store import Connection


async def resolve_webhook(provider: str, external_id: str, config: DatabaseConfig | None = None) -> Connection | None:
    """Resolve a verified provider webhook; unknown or stale routes return no connection.

    The ingress adapter must authenticate the provider before calling this boundary.
    Tenant identity comes exclusively from the stored route, never webhook payloads.
    """
    database = PERSISTENCE.database(config)
    try:
        async with database.connect() as connection:
            owner = (await connection.execute(connection_routes.select().with_only_columns(
                connection_routes.c.tenant_id,
            ).where(
                connection_routes.c.provider == provider,
                connection_routes.c.external_id == external_id,
            ))).scalar_one_or_none()
        if owner is None:
            return None
        scoped = PERSISTENCE.open(config)
        try:
            return await scoped.scoped(tenant(owner)).find(provider, external_id)
        finally:
            await scoped.close()
    finally:
        await database.close()
