"""The database layer OpenEngine V2's services share."""

from openengine.persistence.config import Backend, ConfigError, DatabaseConfig
from openengine.persistence.database import BackendNotImplemented, Database, open_database
from openengine.persistence.service import OpenPersistence, ServicePersistence
from openengine.persistence.store import DatabaseStore, Store
from openengine.persistence.tenancy import (
    LOCAL_TENANT,
    InvalidTenant,
    TenantDatabase,
    TenantId,
    Violation,
    assert_tenant_scoped,
    global_table,
    new_id,
    new_tenant,
    parse_tenant,
    tenant_table,
)
from openengine.persistence.types import UtcDateTime

__all__ = [
    "LOCAL_TENANT",
    "Backend",
    "BackendNotImplemented",
    "ConfigError",
    "Database",
    "DatabaseStore",
    "DatabaseConfig",
    "InvalidTenant",
    "OpenPersistence",
    "ServicePersistence",
    "Store",
    "TenantDatabase",
    "TenantId",
    "UtcDateTime",
    "Violation",
    "assert_tenant_scoped",
    "global_table",
    "new_id",
    "new_tenant",
    "open_database",
    "parse_tenant",
    "tenant_table",
]
