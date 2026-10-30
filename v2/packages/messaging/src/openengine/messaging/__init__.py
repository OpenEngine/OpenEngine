"""Request/reply over NATS, shared by the CLI and the services.

Every handled request carries a verified `Principal`, the tenant it belongs
to, derived by the server from its tenancy mode: dedicated (one tenant, from
configuration; what `oe up` runs as LOCAL_TENANT) or shared (every tenant, the
account key NATS writes into the subject resolved through an `AccountResolver`).
See `tenancy` for the two modes, `server` for serving and `client` for calling
on a tenant's behalf.
"""

from openengine.messaging.client import Client
from openengine.messaging.errors import ErrorCode, ServiceError, Unavailable
from openengine.messaging.server import Authorizer, LocalOperator, Request, Route, Server, listen_address
from openengine.messaging.tenancy import (
    LOCAL_TENANT,
    AccountResolver,
    CachedResolver,
    Dedicated,
    InMemoryAccounts,
    Mode,
    Principal,
    Shared,
    Tenancy,
    TenancyError,
)

__all__ = [
    "LOCAL_TENANT",
    "AccountResolver",
    "Authorizer",
    "CachedResolver",
    "Client",
    "Dedicated",
    "ErrorCode",
    "InMemoryAccounts",
    "LocalOperator",
    "Mode",
    "Principal",
    "Request",
    "Route",
    "Server",
    "ServiceError",
    "Shared",
    "Tenancy",
    "TenancyError",
    "Unavailable",
    "listen_address",
]
