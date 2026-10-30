"""Which tenant a request belongs to, and how a server or client learns it.

A tenant is a NATS account (`common/v1/types.schema.json`: `tenant_id`,
`account`, `local_tenant`). The tenant must come from something the caller
cannot forge, so it is never a payload field. A service learns it in one of
two ways, its *tenancy mode*:

- **Dedicated** (the default, and what `oe up` runs): the service is
  connected inside one tenant's account, so every request it sees belongs to
  that tenant. The tenant comes from configuration, LOCAL_TENANT by default,
  and the subjects stay the plain `oe.v1.{svc}.{query,control}.>`.
- **Shared**: the service runs in the service account and serves every
  tenant. NATS rewrites a tenant's request into the internal form
  `oe.v1.{account}.{svc}.…` as it crosses into the service account
  (`account_token_position: 3`), so the third subject token is the caller's
  account key. An `AccountResolver` maps that key to a tenant; an unknown or
  malformed account is PERMISSION_DENIED.

Either way the server hands its handlers a verified `Principal` as
`Request.principal`. A client acting for a tenant passes that principal on:
in shared mode it addresses `oe.v1.{account}.{svc}.…` itself, in dedicated
mode the tenant's account import carries the plain subject.

Tenant IDs are checked against `tenant_id`, so the nil UUID is refused both
when a mode is configured and when a resolver answers with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from openengine.messaging import envelopes

TYPES = "common/v1/types.schema.json"
LOCAL_TENANT = "00000000-0000-7000-8000-000000000001"
"""The fixed tenant `oe up` runs as, `local_tenant` in the contracts."""
ACCOUNT_POSITION = 2
"""The index of the account key in `oe.v1.{account}.{svc}.…`: NATS writes it as the third token."""


class Mode(StrEnum):
    DEDICATED = "dedicated"
    SHARED = "shared"


class TenancyError(ValueError):
    """A tenant ID, account key or subject that does not fit the tenancy contracts."""


class NotATenantId(TenancyError):
    def __init__(self, description: str) -> None:
        super().__init__(f"not a tenant_id: {description}")


class NilTenantId(TenancyError):
    def __init__(self) -> None:
        super().__init__("the nil UUID is never a tenant")


class NotAnAccount(TenancyError):
    def __init__(self, description: str) -> None:
        super().__init__(f"not an account key: {description}")


class NotAnOeSubject(TenancyError):
    def __init__(self, subject: str) -> None:
        super().__init__(f"not an oe.v1 subject: {subject}")


class SharedClientNeedsAccount(TenancyError):
    def __init__(self) -> None:
        super().__init__("a shared-mode client needs a principal with an account key to address")


@dataclass(frozen=True)
class Principal:
    """Whom a request is for: its tenant, and the account key it arrived through where one is known."""

    tenant_id: str
    account: str | None = None

    def __post_init__(self) -> None:
        check_tenant_id(self.tenant_id)
        if self.account is not None:
            check_account(self.account)


def check_tenant_id(tenant_id: object) -> str:
    """`tenant_id` if it is a well-formed, non-nil tenant ID; raise TenancyError otherwise."""
    if found := envelopes.violations(f"{TYPES}#/$defs/tenant_id", tenant_id):
        raise NotATenantId(found[0][1])
    if set(str(tenant_id)) <= set("0-"):
        raise NilTenantId
    return str(tenant_id)


def check_account(account: object) -> str:
    """`account` if it is a well-formed account public key; raise TenancyError otherwise."""
    if found := envelopes.violations(f"{TYPES}#/$defs/account", account):
        raise NotAnAccount(found[0][1])
    return str(account)


def is_account(token: str) -> bool:
    return not envelopes.violations(f"{TYPES}#/$defs/account", token)


LOCAL_PRINCIPAL = Principal(LOCAL_TENANT)


class AccountResolver(Protocol):
    """The tenancy registry's port: which tenant an account key belongs to."""

    async def resolve(self, account: str) -> str | None:
        """The `tenant_id` of the tenant whose account `account` is, or None when no tenant has it."""


@dataclass
class InMemoryAccounts:
    """A resolver over a fixed mapping of account keys to tenant IDs, for tests and single-process setups."""

    tenants: dict[str, str] = field(default_factory=dict)

    async def resolve(self, account: str) -> str | None:
        return self.tenants.get(account)


@dataclass
class CachedResolver:
    """Remembers each account's tenant once the inner resolver has found it.

    Only found tenants are kept: an account that is unknown today may be
    provisioned tomorrow, and asking again is cheap compared to refusing it.
    Every answer is checked against `tenant_id`, so a resolver cannot hand
    out the nil UUID or anything else that is not a tenant.
    """

    inner: AccountResolver
    _tenants: dict[str, str] = field(default_factory=dict, init=False)

    async def resolve(self, account: str) -> str | None:
        if (tenant_id := self._tenants.get(account)) is None:
            found = await self.inner.resolve(account)
            if found is None:
                return None
            tenant_id = self._tenants[account] = check_tenant_id(found)
        return tenant_id


@dataclass(frozen=True)
class Dedicated:
    """Serve one tenant from inside its account; the tenant comes from configuration."""

    tenant_id: str = LOCAL_TENANT

    def __post_init__(self) -> None:
        check_tenant_id(self.tenant_id)

    @property
    def mode(self) -> Mode:
        return Mode.DEDICATED


@dataclass(frozen=True)
class Shared:
    """Serve every tenant from the service account, reading the account key NATS wrote into the subject."""

    resolver: AccountResolver

    def __post_init__(self) -> None:
        if not isinstance(self.resolver, CachedResolver):
            object.__setattr__(self, "resolver", CachedResolver(self.resolver))

    @property
    def mode(self) -> Mode:
        return Mode.SHARED


Tenancy = Dedicated | Shared


def shared_subject(subject: str, account: str) -> str:
    """The internal form of a plain `oe.v1.{svc}.…` subject for `account`: `oe.v1.{account}.{svc}.…`."""
    return _with_third_token(subject, account)


def subject_template(subject: str) -> str:
    """The plain subject with the `{account}` placeholder where the key goes; for span names, never the key itself."""
    return _with_third_token(subject, "{account}")


def _with_third_token(subject: str, token: str) -> str:
    tokens = subject.split(".")
    if tokens[:ACCOUNT_POSITION] != ["oe", "v1"] or len(tokens) <= ACCOUNT_POSITION:
        raise NotAnOeSubject(subject)
    return ".".join(tokens[:ACCOUNT_POSITION] + [token] + tokens[ACCOUNT_POSITION:])
