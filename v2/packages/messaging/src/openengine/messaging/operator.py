"""Provision operator-mode NATS for the shared deployment.

The shared deployment runs nats-server in operator mode: an operator signs
accounts, the server resolves accounts from signed JWTs, and users connect with
`.creds` issued by their account. This module generates the operator, the
system account and the one service account that holds every shared service
(cxn, ge, tools, vcp), as ed25519 nkeys and signed JWTs, and writes the
operator-mode `nats-server.conf` with a memory resolver that preloads them.

The service account exports each service's `control` and `query` subjects as
services and its `events` as a stream, all in the internal form
`oe.v1.*.{svc}.…` with `account_token_position: 3`: a tenant account can
import them only with its own key as the third token, which is how a shared
service sees the caller's account in the subject and never a forged one.
Webhook subjects (`cxn.ingress.>`, `cxn.egress.>`) stay inside the service
account and are never exported.

Per-tenant accounts are provisioned elsewhere (#1237); `tenant_imports`
gives the imports such an account needs, and `Deployment.account` signs any
extra account into the preload, which the live tests use for a test tenant.
`oe up` does not use this: it keeps the single-account `infra/nats/nats-server.conf`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import nkeys

SERVICES = ("cxn", "ge", "tools", "vcp")
"""The shared services the service account holds and issues credentials for."""
ACCOUNT_TOKEN_POSITION = 3
"""The token of `oe.v1.{account}.{svc}.…` that carries the caller's account key."""
SYSTEM_ACCOUNT = "SYS"
SERVICE_ACCOUNT = "service"
UNLIMITED = -1

OPERATOR_FILE = "operator.jwt"
CONFIG_FILE = "nats-server.conf"


@dataclass(frozen=True)
class Identity:
    """An nkey with the JWT that names it, for an operator, an account or a user."""

    name: str
    keys: nkeys.KeyPair
    jwt: str

    @property
    def public_key(self) -> str:
        return public_key(self.keys)

    @property
    def seed(self) -> str:
        return self.keys.seed.decode()


@dataclass
class Deployment:
    """One operator-mode deployment: the operator, its accounts and users, before or after writing."""

    operator: Identity
    system: Identity
    service: Identity
    accounts: list[Identity] = field(default_factory=list)
    """Every account the resolver preloads besides the system and service accounts."""
    users: list[Identity] = field(default_factory=list)
    """Every user with issued credentials; the account each belongs to is its JWT's issuer."""

    @classmethod
    def generate(cls) -> Deployment:
        """A fresh operator, system account, service account and one user per shared service."""
        operator_keys, system_keys, service_keys = (generate_keys(prefix) for prefix in (
            nkeys.PREFIX_BYTE_OPERATOR, nkeys.PREFIX_BYTE_ACCOUNT, nkeys.PREFIX_BYTE_ACCOUNT,
        ))
        operator = Identity("operator", operator_keys, encode(
            operator_claims(operator_keys, system_account=public_key(system_keys)), operator_keys,
        ))
        system = Identity(SYSTEM_ACCOUNT, system_keys, encode(
            account_claims(system_keys, SYSTEM_ACCOUNT), operator_keys,
        ))
        service = Identity(SERVICE_ACCOUNT, service_keys, encode(
            account_claims(service_keys, SERVICE_ACCOUNT, exports=service_exports(), jetstream=True), operator_keys,
        ))
        deployment = cls(operator, system, service)
        for name in SERVICES:
            deployment.user(service, name)
        return deployment

    def account(self, name: str, keys: nkeys.KeyPair, *, imports: Sequence[dict[str, Any]] = ()) -> Identity:
        """Sign `keys` into an account the resolver preloads, with the given imports."""
        account = Identity(name, keys, encode(account_claims(keys, name, imports=list(imports)), self.operator.keys))
        self.accounts.append(account)
        return account

    def user(self, account: Identity, name: str) -> Identity:
        """Issue a user in `account`, allowed every subject the account can reach."""
        keys = generate_keys(nkeys.PREFIX_BYTE_USER)
        user = Identity(name, keys, encode(user_claims(keys, name), account.keys))
        self.users.append(user)
        return user

    def preload(self) -> dict[str, str]:
        """The resolver's accounts by public key."""
        return {account.public_key: account.jwt for account in (self.system, self.service, *self.accounts)}

    def write(self, directory: Path) -> Written:
        """Write the JWTs, seeds, `nats-server.conf` and one `.creds` per user under `directory`."""
        directory.mkdir(parents=True, exist_ok=True)
        operator_jwt = directory / OPERATOR_FILE
        operator_jwt.write_text(self.operator.jwt + "\n")
        _write_secret(directory / "operator.nk", self.operator.seed + "\n")
        for account in (self.system, self.service, *self.accounts):
            (directory / f"{account.name}.jwt").write_text(account.jwt + "\n")
            _write_secret(directory / f"{account.name}.nk", account.seed + "\n")
        creds = {}
        for user in self.users:
            creds[user.name] = directory / f"{user.name}.creds"
            _write_secret(creds[user.name], credentials(user))
        config = directory / CONFIG_FILE
        config.write_text(server_config(operator_jwt, self.system.public_key, self.preload()))
        return Written(directory, config, creds)


@dataclass(frozen=True)
class Written:
    directory: Path
    config: Path
    creds: dict[str, Path]
    """Each user's `.creds` file by user name."""


def provision(directory: Path) -> tuple[Deployment, Written]:
    """Generate a deployment and write it under `directory`."""
    deployment = Deployment.generate()
    return deployment, deployment.write(directory)


# --- subjects -----------------------------------------------------------------


def service_exports() -> list[dict[str, Any]]:
    """The service account's exports: every shared service's subjects, keyed by caller account at token 3."""
    exports = []
    for service in SERVICES:
        for kind in ("control", "query"):
            exports.append(_export(service, kind, "service"))
        exports.append(_export(service, "events", "stream"))
    return exports


def _export(service: str, kind: str, type_: str) -> dict[str, Any]:
    return {
        "name": f"{service}.{kind}",
        "subject": f"oe.v1.*.{service}.{kind}.>",
        "type": type_,
        "account_token_position": ACCOUNT_TOKEN_POSITION,
    }


def tenant_imports(service_account: str, tenant: str) -> list[dict[str, Any]]:
    """The imports a tenant account makes: `oe.v1.{svc}.…` locally, mapped to `oe.v1.{tenant}.{svc}.…`.

    NATS accepts each only when `tenant`, the third token of the imported
    subject, is the importing account's own key.
    """
    imports = []
    for export in service_exports():
        service, kind = export["name"].split(".")
        imports.append({
            "name": export["name"],
            "account": service_account,
            "type": export["type"],
            "subject": f"oe.v1.{tenant}.{service}.{kind}.>",
            "local_subject": f"oe.v1.{service}.{kind}.>",
        })
    return imports


# --- claims -------------------------------------------------------------------


def operator_claims(keys: nkeys.KeyPair, *, system_account: str) -> dict[str, Any]:
    return _claims(keys, "operator", {"type": "operator", "version": 2, "system_account": system_account})


def account_claims(
    keys: nkeys.KeyPair,
    name: str,
    *,
    exports: list[dict[str, Any]] | None = None,
    imports: list[dict[str, Any]] | None = None,
    jetstream: bool = False,
) -> dict[str, Any]:
    limits: dict[str, Any] = {
        "subs": UNLIMITED, "data": UNLIMITED, "payload": UNLIMITED, "conn": UNLIMITED, "leaf": UNLIMITED,
        "imports": UNLIMITED, "exports": UNLIMITED, "wildcards": True,
    }
    if jetstream:
        limits.update({"mem_storage": UNLIMITED, "disk_storage": UNLIMITED, "streams": UNLIMITED, "consumer": UNLIMITED})
    nats: dict[str, Any] = {"type": "account", "version": 2, "limits": limits, "default_permissions": {"pub": {}, "sub": {}}}
    if exports:
        nats["exports"] = exports
    if imports:
        nats["imports"] = imports
    return _claims(keys, name, nats)


def user_claims(keys: nkeys.KeyPair, name: str) -> dict[str, Any]:
    nats = {"type": "user", "version": 2, "pub": {}, "sub": {}, "subs": UNLIMITED, "data": UNLIMITED, "payload": UNLIMITED}
    return _claims(keys, name, nats)


def _claims(keys: nkeys.KeyPair, name: str, nats: dict[str, Any]) -> dict[str, Any]:
    return {"iat": int(time.time()), "name": name, "sub": public_key(keys), "nats": nats}


# --- nkeys and JWTs -----------------------------------------------------------


def generate_keys(prefix: int) -> nkeys.KeyPair:
    """A new key pair of the given role (`nkeys.PREFIX_BYTE_OPERATOR`, `_ACCOUNT` or `_USER`)."""
    return nkeys.from_seed(bytearray(nkeys.encode_seed(secrets.token_bytes(32), prefix)))


def public_key(keys: nkeys.KeyPair) -> str:
    return keys.public_key.decode()


def encode(claims: dict[str, Any], issuer: nkeys.KeyPair) -> str:
    """`claims` as a NATS JWT signed by `issuer`, with `iss` and `jti` filled in."""
    body = {**claims, "iss": public_key(issuer)}
    body.pop("jti", None)
    digest = hashlib.sha256(_json(body)).digest()
    body = {"jti": base64.b32encode(digest).decode().rstrip("="), **body}
    header = _b64url(_json({"typ": "JWT", "alg": "ed25519-nkey"}))
    payload = _b64url(_json(body))
    signature = issuer.sign(f"{header}.{payload}".encode())
    return f"{header}.{payload}.{_b64url(signature)}"


def decode(token: str) -> dict[str, Any]:
    """A JWT's claims, without checking its signature."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# --- files ----------------------------------------------------------------------


def credentials(user: Identity) -> str:
    """The `.creds` text clients connect with: the user's JWT and its seed."""
    return (
        "-----BEGIN NATS USER JWT-----\n"
        f"{user.jwt}\n"
        "------END NATS USER JWT------\n"
        "\n"
        "************************* IMPORTANT *************************\n"
        "NKEY Seed printed below can be used to sign and prove identity.\n"
        "NKEYs are sensitive and should be treated as secrets.\n"
        "\n"
        "-----BEGIN USER NKEY SEED-----\n"
        f"{user.seed}\n"
        "------END USER NKEY SEED------\n"
        "\n"
        "*************************************************************\n"
    )


def server_config(operator_jwt: Path, system_account: str, preload: dict[str, str]) -> str:
    """An operator-mode nats-server.conf resolving `preload` from memory.

    Like `infra/nats/nats-server.conf`, it leaves the listen address, ports and
    JetStream store directory to the command line.
    """
    accounts = "".join(f"  {key}: {jwt}\n" for key, jwt in preload.items())
    return (
        "# Operator-mode NATS for the shared deployment, generated by openengine.messaging.operator.\n"
        "server_name: openengine\n"
        "jetstream: enabled\n"
        f"operator: {json.dumps(str(operator_jwt))}\n"
        f"system_account: {system_account}\n"
        "resolver: MEMORY\n"
        f"resolver_preload: {{\n{accounts}}}\n"
    )


def _write_secret(path: Path, text: str) -> None:
    """Write `text` readable by the owner only, also when `path` already exists.

    The mode given to `os.open` applies only on creation, so a reprovisioned
    file is narrowed explicitly before the secret is written into it.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


# --- command line -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`provision DIR` writes a deployment; `show DIR` prints the service account's exports."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in ("provision", "show"):
        print("usage: python -m openengine.messaging.operator {provision|show} DIRECTORY", file=sys.stderr)
        return 2
    command, directory = args[0], Path(args[1])
    if command == "provision":
        deployment, written = provision(directory)
        print(f"operator        {deployment.operator.public_key}")
        print(f"system account  {deployment.system.public_key}")
        print(f"service account {deployment.service.public_key}")
        print(f"config          {written.config}")
        for name, path in written.creds.items():
            print(f"{name:<15} {path}")
        return 0
    claims = decode((directory / f"{SERVICE_ACCOUNT}.jwt").read_text().strip())
    print(f"service account {claims['sub']}")
    for export in claims["nats"].get("exports", []):
        print(f"{export['type']:<8}{export['subject']:<32}account_token_position: {export.get('account_token_position')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
