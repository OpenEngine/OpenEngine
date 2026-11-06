"""Repository public contracts over Server, with real checkouts and reopened GE storage."""
import asyncio
import json
import subprocess

from openengine.messaging import LocalOperator, Server, ServiceError, ErrorCode
from openengine.persistence import DatabaseConfig
from openengine.contracts.validate import load_contract, validate_message
from graph_engine.persistence import PERSISTENCE
from graph_engine.registry import Registry

PREFIX = "oe.v1.ge."


def checkout(path):
    path.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.STDOUT).decode().strip()
    git("init", "-b", "main")
    (path / "tracked").write_text("original")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    (path / "tracked").write_text("dirty")
    (path / "untracked").write_text("keep")
    git("worktree", "add", str(path.parent / (path.name + "-held")), "-b", "held")
    return git


def test_registration_restart_and_read_only_discovery(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    git_a, git_b = checkout(first), checkout(second)
    before = [(g("status", "--porcelain"), g("rev-parse", "HEAD"), g("worktree", "list", "--porcelain")) for g in (git_a, git_b)]
    config = DatabaseConfig(sqlite_directory=tmp_path / "db")
    PERSISTENCE.upgrade(config)

    async def exercise():
        accepted_by_project = {}
        for restart in (False, True):
            store = PERSISTENCE.open(config)
            registry = Registry(store)
            server = Server(None, "ge", "nats://127.0.0.1:4222", queries=registry.queries(), commands=registry.commands(), authorizer=LocalOperator())
            async def send(operation, body):
                subject = PREFIX + operation
                return json.loads(await server.handle(subject, json.dumps(body).encode()))
            for project, path, base, trust in [("alpha", first, "main", None), ("beta", second, "HEAD", True)]:
                body = {"command_id": "cmd_" + project, "causal_path": ["cmd_" + project], "payload": {
                    "project": project, "checkout": str(path), "base_ref": base, "workspace_policy": "worktree"}}
                if trust is not None:
                    body["payload"]["trusted"] = trust
                accepted = await send("control.repositories.add", body)
                assert accepted["status"] == "accepted", accepted
                if restart:
                    assert accepted == accepted_by_project[project]
                accepted_by_project[project] = accepted
                assert await send("control.repositories.add", body) == accepted
                changed = json.loads(json.dumps(body))
                changed["payload"]["base_ref"] = "held"
                assert (await send("control.repositories.add", changed))["error"]["code"] == "COMMAND_ID_CONFLICT"
                for action in ("get", "list"):
                    reply = await send("query.repositories." + action, {"params": {"project": project}})
                    assert validate_message(load_contract("graph_engine"), action.title()+"RepositoryResponse", reply, in_reply_to=PREFIX+"query.repositories."+action) == []
                    row = reply["result"]["repository"] if action == "get" else reply["result"]["items"][0]
                    assert row == {"project": project, "checkout": str(path), "base_ref": base, "workspace_policy": "worktree", "trusted": trust}
                if restart:
                    operation = await send("query.operations." + accepted["operation_id"] + ".get", {"params": {}})
                    assert validate_message(load_contract("graph_engine"), "GetOperationResponse", operation, in_reply_to=PREFIX+"query.operations."+accepted["operation_id"]+".get") == []
                    assert operation["result"]["operation"]["result"]["repository"]["trusted"] == trust
            assert (await send("query.repositories.get", {"params": {"project": "missing"}}))["error"]["code"] == "NOT_FOUND"
            await store.close()
    asyncio.run(exercise())
    assert [(g("status", "--porcelain"), g("rev-parse", "HEAD"), g("worktree", "list", "--porcelain")) for g in (git_a, git_b)] == before
    assert (first / "tracked").read_text() == "dirty"
    assert (second / "untracked").read_text() == "keep"


def test_project_authorization_before_discovery(tmp_path):
    class Deny(LocalOperator):
        def authorize_project(self, request, project):
            raise ServiceError(ErrorCode.PERMISSION_DENIED, "denied")
    async def exercise():
        config = DatabaseConfig(sqlite_directory=tmp_path)
        PERSISTENCE.upgrade(config)
        store = PERSISTENCE.open(config)
        authorizer = Deny()
        registry = Registry(store, authorizer)
        server = Server(None, "ge", "nats://127.0.0.1:4222", queries=registry.queries(), commands=registry.commands(), authorizer=authorizer)
        for operation, body in [("control.repositories.add", {"command_id":"cmd_denied", "causal_path":["cmd_denied"], "payload":{"project":"secret"}}), ("query.repositories.get", {"params":{"project":"secret"}}), ("query.repositories.list", {"params":{"project":"secret"}})]:
            reply = json.loads(await server.handle(PREFIX+operation, json.dumps(body).encode()))
            assert reply["error"]["code"] == "PERMISSION_DENIED"
        await store.close()
    asyncio.run(exercise())


def test_invalid_imported_trust_and_tenant_isolation(tmp_path):
    from openengine.persistence import LOCAL_TENANT, new_tenant
    path = tmp_path / "repo"
    checkout(path)
    (path / "engine.toml").write_text('[repos.alpha]\ntrusted = true\n')
    async def exercise():
        config = DatabaseConfig(sqlite_directory=tmp_path / "db")
        PERSISTENCE.upgrade(config)
        store = PERSISTENCE.open(config)
        registry = Registry(store)
        server = Server(None, "ge", "nats://127.0.0.1:4222", queries=registry.queries(), commands=registry.commands(), authorizer=LocalOperator())
        async def add(id, **changes):
            payload = {"project":"alpha", "checkout":str(path), "base_ref":"main", "workspace_policy":"worktree"} | changes
            body = {"command_id":id, "causal_path":[id], "payload":payload}
            return json.loads(await server.handle(PREFIX+"control.repositories.add", json.dumps(body).encode()))
        assert (await add("cmd_valid"))["status"] == "accepted"
        assert (await store.scoped(LOCAL_TENANT).repositories("alpha"))[0]["trusted"] is None
        assert await store.scoped(new_tenant()).repositories("alpha") == []
        moved = path.with_name("moved")
        path.rename(moved)
        assert (await add("cmd_valid"))["status"] == "accepted"
        moved.rename(path)
        for index, changes in enumerate([{"base_ref":"missing"}, {"base_ref":"--help"}, {"checkout":str(tmp_path)}, {"trusted":"true"}, {"workspace_policy":"borrow"}, {"tenant_id":str(new_tenant())}]):
            reply = await add("cmd_bad_"+str(index), **changes)
            assert reply["status"] == "rejected" and reply["error"]["code"] == "INVALID_ARGUMENT", reply
            # Rejected commands remain replayable.
            assert await add("cmd_bad_"+str(index), **changes) == reply
        await store.close()
    asyncio.run(exercise())


def test_transient_discovery_can_retry_same_command(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from graph_engine import repositories
    path = tmp_path / "repo"
    checkout(path)

    async def exercise(failure):
        config = DatabaseConfig(sqlite_directory=tmp_path / type(failure).__name__)
        PERSISTENCE.upgrade(config)
        store = PERSISTENCE.open(config)
        registry = Registry(store)
        server = Server(None, "ge", "nats://127.0.0.1:4222",
                        commands=registry.commands(), authorizer=LocalOperator())
        body = {"command_id": "cmd_retry", "causal_path": ["cmd_retry"], "payload": {
            "project": "alpha", "checkout": str(path), "base_ref": "main", "workspace_policy": "worktree"}}
        async def send():
            return json.loads(await server.handle(PREFIX+"control.repositories.add", json.dumps(body).encode()))
        with monkeypatch.context() as patch:
            if isinstance(failure, OSError):
                patch.setattr(repositories.asyncio, "create_subprocess_exec", AsyncMock(side_effect=failure))
            else:
                async def timeout(awaitable, seconds):
                    awaitable.close()
                    raise TimeoutError
                patch.setattr(repositories.asyncio, "wait_for", timeout)
            reply = await send()
            assert reply["error"]["code"] == "UNAVAILABLE" and reply["error"]["retryable"]
        assert (await send())["status"] == "accepted"
        await store.close()
    for failure in (OSError("temporary"), TimeoutError()):
        asyncio.run(exercise(failure))
