"""Trusted process configuration for public service messaging.

The account mapping is deployment input, never a request field. Broker account
imports and permissions must restrict who can publish account-scoped subjects.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from nats.aio.client import Client as NATS

from openengine.messaging.client import Client
from openengine.messaging.errors import ErrorCode, ServiceError
from openengine.messaging.server import LocalOperator, Request
from openengine.messaging.tenancy import (
    LOCAL_TENANT, Dedicated, Shared, Tenancy, Principal, check_account, check_tenant_id,
)


def _refuse(message: str) -> NoReturn:
    raise ValueError(message)


@dataclass(frozen=True)
class ConfiguredAccounts:
    tenants: Mapping[str, str]

    async def resolve(self, account: str) -> str | None:
        return self.tenants.get(account)


class CredentialedOperator:
    """Trust broker credentials and account routing; allow all projects within the verified tenant.

    This is an explicit deployment choice, not end-user project RBAC.
    """

    def __init__(self, connection: NATS | None = None):
        self.connection = connection

    def authorize(self, request: Request) -> Principal:
        # INFO is refreshed by nats-py on reconnect. A credentials path alone
        # does not prove the broker challenged the connection. Fail closed.
        if self.connection is None or self.connection._server_info.get("auth_required") is not True:
            raise ServiceError(ErrorCode.PERMISSION_DENIED, "credentialed authorization requires broker authentication")
        if request.principal is None:
            raise ServiceError(ErrorCode.PERMISSION_DENIED, "verified principal required")
        return request.principal

    def authorize_project(self, request: Request, project: str) -> None:
        self.authorize(request)


@dataclass(frozen=True)
class Deployment:
    tenancy: Tenancy
    credentials: str | None = None
    authorization: str = "local"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> Deployment:
        env = os.environ if environment is None else environment
        mode = env.get("OE_TENANCY_MODE", "dedicated")
        authorization = env.get("OE_AUTHORIZATION", "local")
        credentials = env.get("OE_NATS_CREDENTIALS") or None
        if authorization not in ("local", "credentialed"):
            _refuse("OE_AUTHORIZATION must be local or credentialed")
        if credentials and not Path(credentials).is_file():
            _refuse("OE_NATS_CREDENTIALS must name a credentials file")
        if authorization == "credentialed" and not credentials:
            _refuse("credentialed authorization requires OE_NATS_CREDENTIALS")
        if mode == "dedicated":
            if authorization == "credentialed" and not env.get("OE_TENANT_ID"):
                _refuse("credentialed dedicated mode requires explicit OE_TENANT_ID")
            tenancy = Dedicated(env.get("OE_TENANT_ID", LOCAL_TENANT))
        elif mode == "shared":
            if authorization != "credentialed":
                _refuse("shared mode requires explicit credentialed authorization")
            mapping = json.loads(env.get("OE_ACCOUNT_TENANTS", "{}"))
            if not isinstance(mapping, dict) or not mapping:
                _refuse("OE_ACCOUNT_TENANTS must be a nonempty account-to-tenant object")
            tenants = {check_account(account): check_tenant_id(tenant) for account, tenant in mapping.items()}
            tenancy = Shared(ConfiguredAccounts(tenants))
        else:
            _refuse("OE_TENANCY_MODE must be dedicated or shared")
        return cls(tenancy, credentials, authorization)

    def connection_options(self) -> dict[str, str]:
        return {"user_credentials": self.credentials} if self.credentials else {}

    def authorizer(self, connection: NATS | None = None) -> LocalOperator | CredentialedOperator:
        return LocalOperator() if self.authorization == "local" else CredentialedOperator(connection)

    def public_client(self, connection: NATS, request: Request) -> Client:
        """Bind outbound public calls to this request's verified identity and deployment mode."""
        if request.principal is None:
            _refuse("verified principal required")
        return Client(connection, principal=request.principal, tenancy=self.tenancy.mode)
