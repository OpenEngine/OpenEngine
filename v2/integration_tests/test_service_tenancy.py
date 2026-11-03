"""Deployment configuration at the real public NATS service boundary."""
import asyncio
import json
import subprocess
import time

import nats
import pytest

from openengine.messaging import Route, Server, Principal
from openengine.messaging.deployment import Deployment
from openengine.supervisor.processes.nats import install_nats_server
from integration_tests.test_ge import _free_port, _oe, _up

ACCOUNT = "A" + "B" * 55
TENANT = "0192f1a0-5b3c-7def-8abc-0123456789ab"
SUBJECT = "oe.v1.ge.query.graphs.list"


def configuration(tmp_path, mode):
    credentials = tmp_path / "tenant.creds"
    credentials.touch()
    return Deployment.from_environment({
        "OE_TENANCY_MODE": mode, "OE_TENANT_ID": TENANT,
        "OE_NATS_CREDENTIALS": str(credentials), "OE_AUTHORIZATION": "credentialed",
        "OE_ACCOUNT_TENANTS": json.dumps({ACCOUNT: TENANT}),
    })


@pytest.mark.parametrize("service", ["ge", "vcp"])
@pytest.mark.parametrize("mode", ["dedicated", "shared"])
def test_configured_routing_and_initiating_principal_preserved(tmp_path, service, mode):
    deployment = configuration(tmp_path, mode)
    binary = install_nats_server(tmp_path / "cache")
    port = _free_port()
    process = subprocess.Popen([str(binary), "-a", "127.0.0.1", "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def exercise():
        connection = await nats.connect(f"nats://127.0.0.1:{port}")
        seen = []

        async def downstream(request):
            seen.append(request.principal)
            return {"items": [], "next_cursor": None}

        async def forward(request):
            seen.append(request.principal)
            reply = await deployment.public_client(connection, request).query("oe.v1.tools.query.inspect")
            return reply["result"]

        servers = [Server(connection, service, "nats://127.0.0.1", queries={"inspect": Route(forward)},
                          tenancy=deployment.tenancy, authorizer=deployment.authorizer()),
                   Server(connection, "tools", "nats://127.0.0.1", queries={"inspect": Route(downstream)},
                          tenancy=deployment.tenancy, authorizer=deployment.authorizer())]
        try:
            for server in servers:
                await server.start()
            prefix = f"oe.v1.{ACCOUNT}" if mode == "shared" else "oe.v1"
            subject = f"{prefix}.{service}.query.inspect"
            reply = json.loads((await connection.request(subject, b'{"params": {}}')).data)
            assert reply["status"] == "ok"
            assert seen == [Principal(TENANT, ACCOUNT if mode == "shared" else None)] * 2
            spoof = json.loads((await connection.request(subject, b'{"tenant_id": "spoof", "params": {}}')).data)
            assert spoof["error"]["code"] == "INVALID_ARGUMENT"
            assert len(seen) == 2
            if mode == "shared":
                unknown = subject.replace(ACCOUNT, "A" + "C" * 55)
                refused = json.loads((await connection.request(unknown, b'{"params": {}}')).data)
                assert refused["error"]["code"] == "PERMISSION_DENIED"
        finally:
            await connection.close()
    try:
        time.sleep(0.1)
        asyncio.run(exercise())
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("environment", [
    {"OE_TENANCY_MODE": "invalid"}, {"OE_TENANT_ID": "spoof"},
    {"OE_TENANCY_MODE": "shared"}, {"OE_AUTHORIZATION": "credentialed"},
])
def test_invalid_deployment_refused(environment):
    with pytest.raises(ValueError):
        Deployment.from_environment(environment)


def test_oe_up_overrides_inherited_shared_settings_and_cli_subjects(tmp_path):
    oe = _oe(tmp_path / "state")
    try:
        result = _up(oe, _free_port(), OE_TENANCY_MODE="shared", OE_AUTHORIZATION="credentialed",
                     OE_NATS_CREDENTIALS="/missing", OE_ACCOUNT_TENANTS="invalid")
        assert result.returncode == 0, result.stderr
        result = oe("graph", "list", env={"OE_NATS_CREDENTIALS": ""})
        assert result.returncode == 0, result.stderr
        logs = oe("logs").stdout
        for service in ("ge", "vcp"):
            assert f"serving oe.v1.{service}.query.> in queue {service}" in logs
            assert f"serving oe.v1.{service}.control.> in queue {service}" in logs
    finally:
        oe("down")


def test_cli_uses_tenant_credentials_and_plain_subject(monkeypatch):
    from openengine.cli.commands import graph

    captured = {}

    class Connection:
        async def request(self, subject, payload, **kwargs):
            captured["subject"] = subject
            return type("Reply", (), {"data": b'{"status":"ok","result":{"items":[],"next_cursor":null}}'})()

        async def close(self):
            pass

    async def connect(*args, **kwargs):
        captured.update(kwargs)
        return Connection()

    monkeypatch.setenv("OE_NATS_CREDENTIALS", "/tenant.creds")
    monkeypatch.setattr(graph.nats, "connect", connect)
    asyncio.run(graph._ask("nats://broker", lambda client: client.query(SUBJECT)))
    assert captured["user_credentials"] == "/tenant.creds"
    assert captured["subject"] == SUBJECT


@pytest.mark.parametrize("service", ["graph_engine", "virtual_control_plane"])
@pytest.mark.parametrize("mode", ["dedicated", "shared"])
def test_service_boot_wires_deployment_to_connection_and_server(tmp_path, monkeypatch, service, mode):
    import importlib
    from openengine.messaging.deployment import CredentialedOperator

    module = importlib.import_module(f"{service}.__main__")
    deployment = configuration(tmp_path, mode)
    captured = {}

    class ReachedServer(Exception):
        pass

    class Connection:
        _server_info = {"host": "0.0.0.0", "port": 4222}

    async def connect(*args, **kwargs):
        captured["connection"] = kwargs
        return Connection()

    def server(*args, **kwargs):
        captured["server"] = kwargs
        raise ReachedServer

    monkeypatch.setattr(module.Deployment, "from_environment", lambda: deployment)
    monkeypatch.setattr(module.nats, "connect", connect)
    monkeypatch.setattr(module, "Server", server)
    if service == "virtual_control_plane":
        monkeypatch.setattr(module, "providers", lambda: {})
    with pytest.raises(ReachedServer):
        asyncio.run(module.serve("nats://broker", 0, None, object()))
    assert captured["connection"]["user_credentials"] == deployment.credentials
    assert captured["server"]["tenancy"] is deployment.tenancy
    assert isinstance(captured["server"]["authorizer"], CredentialedOperator)
