"""Operator-mode provisioning: the generated JWTs, the exports' shape, and a live nats-server booted from them."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from urllib.request import urlopen

import nats
import pytest
from nats.errors import NoRespondersError

from openengine.messaging import operator
from openengine.messaging.operator import SERVICES


# --- static ---------------------------------------------------------------------


def test_every_export_is_keyed_by_the_callers_account_at_token_3() -> None:
    exports = operator.service_exports()

    assert len(exports) == 12
    assert {export["subject"] for export in exports} == {
        f"oe.v1.*.{service}.{kind}.>" for service in SERVICES for kind in ("control", "query", "events")
    }
    for export in exports:
        assert export["account_token_position"] == 3, export
        assert export["subject"].split(".")[2] == "*", export
        assert export["type"] == ("stream" if export["subject"].endswith(".events.>") else "service"), export


def test_webhook_subjects_are_never_exported() -> None:
    subjects = [export["subject"] for export in operator.service_exports()]

    assert not [subject for subject in subjects if subject.startswith("cxn.")]
    assert not [subject for subject in subjects if "ingress" in subject or "egress" in subject]


def test_a_tenant_imports_each_export_mapped_from_its_own_key() -> None:
    imports = operator.tenant_imports("ASERVICE", "ATENANT")

    assert len(imports) == 12
    for entry in imports:
        assert entry["account"] == "ASERVICE"
        assert entry["subject"].split(".")[2] == "ATENANT"
        assert entry["subject"].replace(".ATENANT.", ".") == entry["local_subject"]


def test_jwts_are_ed25519_nkey_signed_by_their_issuer() -> None:
    deployment = operator.Deployment.generate()

    for identity, issuer in (
        (deployment.operator, deployment.operator), (deployment.system, deployment.operator),
        (deployment.service, deployment.operator), (deployment.users[0], deployment.service),
    ):
        header, payload, signature = identity.jwt.split(".")
        assert operator.decode(identity.jwt)["iss"] == issuer.public_key
        assert operator.decode(identity.jwt)["sub"] == identity.public_key
        assert issuer.keys.verify(f"{header}.{payload}".encode(), _unpad(signature))
    assert operator.decode(deployment.operator.jwt)["nats"]["system_account"] == deployment.system.public_key
    assert deployment.operator.public_key.startswith("O")
    assert deployment.service.public_key.startswith("A")
    assert {user.name for user in deployment.users} == set(SERVICES)


def test_provision_writes_the_config_resolving_every_account(tmp_path: Path) -> None:
    deployment, written = operator.provision(tmp_path)

    config = written.config.read_text()
    assert f"operator: {tmp_path / 'operator.jwt'}" in config
    assert f"system_account: {deployment.system.public_key}" in config
    assert "resolver: MEMORY" in config
    for account in (deployment.system, deployment.service):
        assert f"  {account.public_key}: {account.jwt}\n" in config
    assert set(written.creds) == set(SERVICES)
    for path in written.creds.values():
        assert path.stat().st_mode & 0o077 == 0, path
        assert "-----BEGIN NATS USER JWT-----" in path.read_text()
    assert (tmp_path / "service.nk").stat().st_mode & 0o077 == 0


def test_the_command_line_reports_what_it_wrote(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert operator.main(["provision", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == ["operator", "system", "service", "config", *SERVICES]

    assert operator.main(["show", str(tmp_path)]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert len(shown) == 13
    assert all(line.endswith("account_token_position: 3") for line in shown[1:])


def _unpad(signature: str) -> bytes:
    return base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))


# --- live -------------------------------------------------------------------------


@pytest.mark.integration
def test_the_server_starts_from_the_generated_jwts_with_a_resolver(operator_broker) -> None:
    deployment = operator_broker.deployment

    with urlopen(f"{operator_broker.monitor}/varz", timeout=5) as response:
        varz = json.load(response)

    assert varz["system_account"] == deployment.system.public_key
    assert [claim["sub"] for claim in varz["trusted_operators_claim"]] == [deployment.operator.public_key]
    assert "Trusted Operators" in operator_broker.log.read_text()


@pytest.mark.integration
def test_a_tenant_import_is_mapped_to_its_own_key_and_a_forged_one_refused(operator_broker) -> None:
    tenant_key = operator_broker.tenant.public_key

    async def scenario() -> tuple[str, str, str, str]:
        service = await nats.connect(operator_broker.url, user_credentials=operator_broker.creds("cxn"))
        tenant = await nats.connect(operator_broker.url, user_credentials=operator_broker.creds("tenant-user"))
        forger = await nats.connect(operator_broker.url, user_credentials=operator_broker.creds("forger-user"))
        try:
            seen: list[str] = []

            async def answer(message: nats.aio.msg.Msg) -> None:
                seen.append(message.subject)
                await message.respond(b"pong")

            await service.subscribe("oe.v1.*.cxn.control.>", cb=answer)
            events = await tenant.subscribe("oe.v1.cxn.events.>")
            await service.flush()
            await tenant.flush()

            reply = await tenant.request("oe.v1.cxn.control.ping", b"", timeout=5)
            await service.publish(f"oe.v1.{tenant_key}.cxn.events.connections.created", b"event")
            event = await events.next_msg(timeout=5)
            with pytest.raises(NoRespondersError):
                await forger.request("oe.v1.cxn.control.ping", b"", timeout=5)
            return reply.data.decode(), seen[0], event.subject, event.data.decode()
        finally:
            for connection in (service, tenant, forger):
                await connection.close()

    reply, seen, event_subject, event = asyncio.run(scenario())

    assert reply == "pong"
    assert seen == f"oe.v1.{tenant_key}.cxn.control.ping"
    assert (event_subject, event) == ("oe.v1.cxn.events.connections.created", "event")
    log = operator_broker.log.read_text()
    assert f"{operator_broker.forger.public_key}/forger]: service import not authorized" in log
    assert f"{operator_broker.forger.public_key}/forger]: stream import not authorized" in log
    assert f"{tenant_key}/tenant]: service import not authorized" not in log


@pytest.mark.integration
@pytest.mark.parametrize("service", SERVICES)
def test_each_services_credentials_connect_and_reach_its_subjects(operator_broker, service: str) -> None:
    tenant_key = operator_broker.tenant.public_key
    subjects = [f"oe.v1.{tenant_key}.{service}.{kind}.example" for kind in ("control", "query", "events")]
    if service == "cxn":
        subjects += ["cxn.ingress.github.push", "cxn.egress.slack.post"]

    async def scenario() -> list[str]:
        connection = await nats.connect(operator_broker.url, user_credentials=operator_broker.creds(service))
        try:
            received = await connection.subscribe(">")
            await connection.flush()
            for subject in subjects:
                await connection.publish(subject, b"")
            return [(await received.next_msg(timeout=5)).subject for _ in subjects]
        finally:
            await connection.close()

    assert asyncio.run(scenario()) == subjects
