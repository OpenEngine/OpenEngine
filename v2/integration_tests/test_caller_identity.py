"""Real NATS authority boundary with synthetic callers and a tiny resource service.

This fixture explicitly uses plaintext loopback to isolate caller authentication.
It does not qualify managed TLS, credential custody, GE receipts or runtime grants.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import nats
import nkeys
import pytest
from nats.errors import Error as NatsError, TimeoutError as NatsTimeoutError

from openengine.messaging import (
    CallerIngress, Client, Principal, Request, Route, Server, ServiceError, caller_subject, envelopes, reply_prefix,
)
from openengine.supervisor.processes.nats import install_nats_server

pytestmark = pytest.mark.integration
A, B = Principal("alice"), Principal("bob")
READ = "oe.v1.ge.query.resources.get"
WRITE = "oe.v1.ge.control.resources.write"


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    return install_nats_server(tmp_path_factory.mktemp("caller-nats-cache"))


class ResourceAuthorizer:
    def authorize(self, request: Request) -> None:
        if request.principal not in {A, B} or request.operation not in {"resources.get", "resources.write"}:
            raise ServiceError("PERMISSION_DENIED", "operation not granted")

    def authorize_project(self, request: Request, project: str) -> None:
        if project != {A: "alpha", B: "beta"}.get(request.principal):
            raise ServiceError("PERMISSION_DENIED", "project not granted")


class Resources:
    """Resolve ownership at the service, including existing command receipts."""

    def __init__(self):
        self.authorizer = ResourceAuthorizer()
        self.receipts = {}
        self.effects = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def read(self, request):
        resource = request.body["params"]["resource"]
        self.authorizer.authorize_project(request, {"a-only": "alpha", "b-only": "beta"}[resource])
        return {"owner": request.principal.id, "resource": resource}

    async def write(self, request):
        resource = request.body["payload"]["resource"]
        self.authorizer.authorize_project(request, {"a-only": "alpha", "b-only": "beta"}[resource])
        command_id = request.body["command_id"]
        if command_id in self.receipts:
            owner, reply = self.receipts[command_id]
            if owner != request.principal:
                raise ServiceError("PERMISSION_DENIED", "receipt belongs to another caller")
            return reply
        if request.body["payload"].get("wait"):
            self.entered.set()
            await self.release.wait()
        self.effects.append((request.principal, command_id))
        reply = {"command_id": command_id, "status": "accepted", "operation_id": "op_1"}
        self.receipts[command_id] = (request.principal, reply)
        return reply


def permissions(publish, subscribe):
    # Empty allow lists mean unrestricted in NATS. Empty grants must deny all.
    return {"publish": {"allow": publish} if publish else {"deny": [">"]},
            "subscribe": {"allow": subscribe} if subscribe else {"deny": [">"]}}


@asynccontextmanager
async def harness(tmp_path, binary, *, broken_binding=False):
    pairs = {name: nkeys.from_seed(nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER))
             for name in ("alice", "bob", "service", "empty")}
    users = []
    for principal in (A, B):
        token = "*" if broken_binding and principal == A else principal.id
        users.append({"nkey": pairs[principal.id].public_key.decode(), "permissions": permissions(
            [f"oe.v1.caller.{token}.ge.query.>", f"oe.v1.caller.{token}.ge.control.>"],
            [reply_prefix(principal) + ".>"],
        )})
    users.append({"nkey": pairs["service"].public_key.decode(), "permissions": permissions(
        ["_INBOX.oe.*.>"], ["oe.v1.caller.*.ge.query.>", "oe.v1.caller.*.ge.control.>"],
    )})
    users.append({"nkey": pairs["empty"].public_key.decode(), "permissions": permissions([], [])})
    config = {"host": "127.0.0.1", "port": -1,
              "authorization": {"default_permissions": permissions([], []), "users": users}}
    config_path, log_path = tmp_path / "nats.json", tmp_path / "nats.log"
    config_path.write_text(json.dumps(config))
    connections = []
    with log_path.open("wb") as log:
        process = subprocess.Popen([str(binary), "-c", str(config_path), "--ports_file_dir", str(tmp_path)], stdout=log, stderr=log)
        try:
            ports = tmp_path / f"nats-server_{process.pid}.ports"
            async with asyncio.timeout(5):
                while not ports.exists():
                    assert process.poll() is None, log_path.read_text()
                    await asyncio.sleep(0.01)
            url = json.loads(ports.read_text())["nats"][0]
            errors = {name: asyncio.Queue() for name in pairs}
            closed = {name: asyncio.Event() for name in pairs}

            async def connect(name):
                async def error_cb(error):
                    await errors[name].put(error)

                connection = nats.aio.client.Client()
                connections.append(connection)
                await connection.connect(
                    url, nkeys_seed_str=pairs[name].seed.decode(),
                    inbox_prefix=reply_prefix(Principal(name)), allow_reconnect=False,
                    error_cb=error_cb, closed_cb=_async_set(closed[name]), connect_timeout=1,
                )
                return connection

            service, a, b = await connect("service"), await connect("alice"), await connect("bob")
            resources = Resources()
            server = Server(service, "ge", url, queries={"resources.get": Route(resources.read)},
                            commands={"resources.write": Route(resources.write)}, authorizer=resources.authorizer,
                            caller_ingress=CallerIngress(frozenset({A, B})))
            await server.start()
            yield SimpleNamespace(a=a, b=b, service=service, server=server, resources=resources,
                                  errors=errors, closed=closed, connect=connect, url=url,
                                  config=config, config_path=config_path, process=process, pairs=pairs)
        finally:
            for connection in reversed(connections):
                await connection.close()
            if process.poll() is None:
                process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
            for pair in pairs.values():
                pair.wipe()


def _async_set(event):
    async def set_event():
        event.set()
    return set_event


async def denied_publish(h, subject):
    await h.a.publish(subject, envelopes.encode(envelopes.query({"resource": "b-only"})))
    await h.a.flush()
    error = await asyncio.wait_for(h.errors["alice"].get(), 1)
    assert "permissions violation for publish" in str(error).lower()


async def denied_subscribe(connection, errors, subject):
    subscription = await connection.subscribe(subject)
    await connection.flush()
    error = await asyncio.wait_for(errors.get(), 1)
    assert "permissions violation for subscription" in str(error).lower()
    await subscription.unsubscribe()


def test_two_callers_resource_and_receipt_ownership(tmp_path, binary):
    async def run():
        async with harness(tmp_path, binary) as h:
            alice, bob = Client(h.a, principal=A), Client(h.b, principal=B)
            assert (await alice.query(READ, {"resource": "a-only"}))["result"]["owner"] == "alice"
            assert (await bob.query(READ, {"resource": "b-only"}))["result"]["owner"] == "bob"
            # Payload actor/project are untrusted selectors, not identity evidence.
            with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                await alice.query(READ, {"resource": "b-only", "actor": "bob", "project": "beta"})
            first = await alice.command(WRITE, {"resource": "a-only"}, command_id="cmd_shared")
            assert await alice.command(WRITE, {"resource": "a-only"}, command_id="cmd_shared") == first
            with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                await bob.command(WRITE, {"resource": "b-only"}, command_id="cmd_shared")
            assert h.resources.effects == [(A, "cmd_shared")]
    asyncio.run(run())


def test_broker_denies_forgery_alternate_ingress_events_admin_and_foreign_inbox(tmp_path, binary):
    async def run():
        async with harness(tmp_path, binary) as h:
            for subject in (caller_subject(B, READ), READ, "oe.v1.ge.event.changed", "$JS.API.INFO", "$SYS.REQ.SERVER.PING"):
                await denied_publish(h, subject)
            for subject in ("_INBOX.oe.bob.>", "oe.v1.ge.event.>", "oe.v1.caller.>", ">"):
                await denied_subscribe(h.a, h.errors["alice"], subject)
            empty = await h.connect("empty")
            await empty.publish(caller_subject(A, READ), b"{}")
            await empty.flush()
            assert "permissions violation for publish" in str(await asyncio.wait_for(h.errors["empty"].get(), 1)).lower()
            await denied_subscribe(empty, h.errors["empty"], ">")
            assert h.resources.effects == []
    asyncio.run(run())


@pytest.mark.parametrize("payload", [b"not-json", envelopes.encode(envelopes.command({"resource": "a-only"}, command_id="cmd_forged"))])
def test_foreign_reply_is_dropped_before_error_or_effect(tmp_path, binary, payload):
    async def run():
        async with harness(tmp_path, binary) as h:
            target = "_INBOX.oe.bob.stolen"
            subscription = await h.b.subscribe(target)
            await h.b.flush()
            await h.a.publish(caller_subject(A, WRITE), payload, reply=target)
            await h.a.flush()
            # A following control request on the same subscription is a processing barrier.
            await Client(h.a, principal=A).command(WRITE, {"resource": "a-only"}, command_id="cmd_barrier")
            with pytest.raises(NatsTimeoutError):
                await subscription.next_msg(timeout=0.05)
            assert h.resources.effects == [(A, "cmd_barrier")]
    asyncio.run(run())


def test_missing_credentials_are_refused(tmp_path, binary):
    async def run():
        async with harness(tmp_path, binary) as h:
            errors = []
            async def error_cb(error):
                errors.append(error)
            connection = nats.aio.client.Client()
            try:
                with pytest.raises(NatsError, match="Authorization Violation"):
                    await connection.connect(h.url, allow_reconnect=False, connect_timeout=1, error_cb=error_cb)
            finally:
                await connection.close()
    asyncio.run(run())


def test_broken_broker_binding_negative_control_impersonates_bob(tmp_path, binary):
    async def run():
        async with harness(tmp_path, binary, broken_binding=True) as h:
            # Deliberately permit Alice to publish Bob's namespace. Bob receives
            # the result: this proves the ingress depends on broker enforcement.
            target = "_INBOX.oe.bob.control"
            subscription = await h.b.subscribe(target)
            await h.b.flush()
            await h.a.publish(caller_subject(B, WRITE), envelopes.encode(envelopes.command(
                {"resource": "b-only"}, command_id="cmd_impersonated")), reply=target)
            reply = json.loads((await subscription.next_msg(timeout=1)).data)
            assert reply["status"] == "accepted"
            assert h.resources.effects == [(B, "cmd_impersonated")]
    asyncio.run(run())


def test_revocation_denies_queued_work_and_disconnects_existing_caller(tmp_path, binary):
    async def run():
        async with harness(tmp_path, binary) as h:
            first = asyncio.create_task(Client(h.a, principal=A).command(
                WRITE, {"resource": "a-only", "wait": True}, command_id="cmd_admitted"))
            try:
                await asyncio.wait_for(h.resources.entered.wait(), 1)
                await h.a.publish(caller_subject(A, WRITE), envelopes.encode(envelopes.command(
                    {"resource": "a-only"}, command_id="cmd_queued")), reply="_INBOX.oe.alice.queued")
                await h.a.flush()
                # Admission cutoff: already-admitted work may complete; queued
                # callbacks must read the replacement snapshot before admission.
                h.server.caller_ingress = CallerIngress(frozenset({B}))
                h.resources.release.set()
                await first
                await Client(h.b, principal=B).command(WRITE, {"resource": "b-only"}, command_id="cmd_barrier")
                assert h.resources.effects == [(A, "cmd_admitted"), (B, "cmd_barrier")]
                alice_key = h.pairs["alice"].public_key.decode()
                h.config["authorization"]["users"] = [u for u in h.config["authorization"]["users"] if u["nkey"] != alice_key]
                h.config_path.write_text(json.dumps(h.config))
                h.process.send_signal(signal.SIGHUP)
                await asyncio.wait_for(h.closed["alice"].wait(), 2)
                with pytest.raises(NatsError, match="Authorization Violation"):
                    await h.connect("alice")
                assert (await Client(h.b, principal=B).query(READ, {"resource": "b-only"}))["status"] == "ok"
            finally:
                h.resources.release.set()
                await first
    asyncio.run(run())


def test_headers_do_not_change_identity_and_own_inbox_receives_errors(tmp_path, binary):
    async def run():
        async with harness(tmp_path, binary) as h:
            response = await h.a.request(caller_subject(A, READ), envelopes.encode(envelopes.query(
                {"resource": "b-only"})), headers={"principal": "bob", "actor": "bob"}, timeout=1)
            assert json.loads(response.data)["error"]["code"] == "PERMISSION_DENIED"
            malformed = await h.a.request(caller_subject(A, READ), b"not-json", timeout=1)
            assert json.loads(malformed.data)["error"]["code"] == "INVALID_ARGUMENT"
            with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
                await Client(h.a, principal=A).query("oe.v1.ge.query.resources.delete")
    asyncio.run(run())


def test_removed_reply_guard_negative_control_sends_error_to_foreign_inbox(tmp_path, binary, monkeypatch):
    verify = CallerIngress.verify

    def without_reply_guard(self, service, subject, reply):
        return verify(self, service, subject, "_INBOX.oe.alice.control")

    monkeypatch.setattr(CallerIngress, "verify", without_reply_guard)

    async def run():
        async with harness(tmp_path, binary) as h:
            target = "_INBOX.oe.bob.stolen"
            subscription = await h.b.subscribe(target)
            await h.b.flush()
            await h.a.publish(caller_subject(A, READ), b"not-json", reply=target)
            response = await subscription.next_msg(timeout=1)
            assert json.loads(response.data)["error"]["code"] == "INVALID_ARGUMENT"
    asyncio.run(run())
