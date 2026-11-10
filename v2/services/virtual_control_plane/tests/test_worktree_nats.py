"""Public NATS: create, rediscover after a VCP restart and destroy an owned process workspace.

`python -m virtual_control_plane` runs as a subprocess with the worktree
adapter configured, against a real `nats-server`, a real temporary Git
repository and the real `python -m workspace_daemon` (fake executor only, on
the explicit local test account). No VM binary, image, guest cgroup or agent
is involved. Runs on macOS and Linux.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from openengine.messaging import Client
from openengine.persistence import DatabaseConfig, LOCAL_TENANT, TenantDatabase

from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.providers.worktree import HostDaemon
from virtual_control_plane.tests.test_lifecycle import broker

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="worktree workspaces run on macOS and Linux")]

CONTROL = "oe.v1.vcp.control.workspaces"
DEADLINE_SECONDS = 60.0


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-C", str(path), *arguments],
                          check=True, capture_output=True, text=True).stdout


async def until(predicate, timeout: float = DEADLINE_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := await predicate():
            return result
        await asyncio.sleep(0.1)
    pytest.fail("condition not reached")


class Vcp:
    """The service as a separate process, restartable over the same state and database."""

    def __init__(self, tmp_path: Path, url: str) -> None:
        self.tmp_path, self.ready = tmp_path, tmp_path / "vcp.ready"
        self.environment = {**os.environ, "OE_NATS_URL": url, "OE_VCP_STATE_DIR": str(tmp_path / "vcp"),
                            "OE_VCP_READY_FILE": str(self.ready), "OE_VCP_HEALTH_PORT": "0",
                            "OE_DATABASE_BACKEND": "sqlite", "OE_SQLITE_DIRECTORY": str(tmp_path / "db"),
                            "OE_VCP_WORKTREE": "1", "OE_VCP_RUNTIME_LOCAL_TEST_ACCOUNT": "1",
                            "OE_VCP_WORKTREE_SOURCE_ROOTS": str(tmp_path / "source")}
        self.process: subprocess.Popen | None = None

    async def start(self) -> None:
        log = (self.tmp_path / "vcp.log").open("a")
        self.process = subprocess.Popen([sys.executable, "-m", "virtual_control_plane"], env=self.environment,
                                        stdout=log, stderr=subprocess.STDOUT)

        async def listening():
            assert self.process.poll() is None, (self.tmp_path / "vcp.log").read_text()
            return self.ready.exists()
        await until(listening)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            assert self.process.wait(timeout=15) == 0


def test_create_rediscover_after_restart_and_destroy_over_public_nats(binary, tmp_path: Path) -> None:
    tmp_path = tmp_path.resolve()
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    (source / "README").write_text("hello\n")
    git(source, "add", ".")
    git(source, "commit", "-q", "-m", "init")
    (source / "README").write_text("the caller's uncommitted edit\n")
    before = (git(source, "status", "--porcelain"), git(source, "for-each-ref"), git(source, "worktree", "list"))
    document = {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": "local"},
                "spec": {"provider": "process", "tools": ["git", "terminal"],
                         "context": {"repositories": [{"url": str(source), "ref": "main"}], "instructions": "Hi.\n"}}}

    async def scenario() -> None:
        async with broker(binary) as connection:
            vcp = Vcp(tmp_path, connection.connected_url.geturl())
            client = Client(connection, timeout=10)
            try:
                await vcp.start()
                capabilities = (await client.query("oe.v1.vcp.query.capabilities.get"))["result"]["providers"]
                assert [(p["provider"], p["isolation"]) for p in capabilities] == [("process", "process")]

                created = await client.command(f"{CONTROL}.apply", {"workspace": document}, command_id="cmd_create")
                workspace_id = created["resource_id"]

                async def workspace(state):
                    current = (await client.query(f"oe.v1.vcp.query.workspaces.{workspace_id}.get"))["result"]["workspace"]
                    return current if current["state"] == state else None
                ready = await until(lambda: workspace("ready"))
                runtime_id = ready["runtime"]["runtime_id"]
                directory = tmp_path / "vcp" / "worktrees" / runtime_id
                tree = directory / "workspace" / "repositories" / "0"
                journal = directory / "state"
                assert (tree / "README").read_text() == "hello\n"
                assert (directory / "workspace" / "AGENTS.md").read_text() == "Hi.\n"
                assert HostDaemon.running(journal)
                pid = json.loads((directory / "daemon.json").read_text())["pid"]

                # The daemon registers through the runtime protocol once the workspace is ready.
                store = DurableStore(TenantDatabase(PERSISTENCE.database(DatabaseConfig(sqlite_directory=tmp_path / "db")), LOCAL_TENANT))
                try:
                    async def registered():
                        status = await store.read("runtime_status", runtime_id)
                        return status and status.get("ready")
                    await until(registered)
                finally:
                    await store.close()

                # VCP restarts; the daemon and worktree outlive it and are rediscovered, not recreated.
                vcp.stop()
                assert HostDaemon.running(journal)
                await vcp.start()
                assert await workspace("ready") == ready
                ensured = await client.command(f"{CONTROL}.{workspace_id}.ensure", {}, command_id="cmd_ensure")

                async def finished(operation_id):
                    operation = (await client.query(f"oe.v1.vcp.query.operations.{operation_id}.get"))["result"]["operation"]
                    return operation if operation["status"] in ("succeeded", "failed") else None
                operation = await until(lambda: finished(ensured["operation_id"]))
                assert operation["status"] == "succeeded" and operation["result"]["runtime_id"] == runtime_id
                assert json.loads((directory / "daemon.json").read_text())["pid"] == pid
                assert (journal / "binding.json").exists()

                destroyed = await client.command(f"{CONTROL}.{workspace_id}.destroy",
                                                 {"retention": {"artifacts": "retain", "checkpoints": "delete"}},
                                                 command_id="cmd_destroy")
                operation = await until(lambda: finished(destroyed["operation_id"]))
                assert operation["status"] == "succeeded", operation
                assert await workspace("destroyed")
                assert not directory.exists() and not HostDaemon.running(journal)
                assert (git(source, "status", "--porcelain"), git(source, "for-each-ref"),
                        git(source, "worktree", "list")) == before
            finally:
                vcp.stop()
                if (record := next((tmp_path / "vcp" / "worktrees").glob("*/daemon.json"), None)) is not None:
                    try:
                        os.killpg(json.loads(record.read_text())["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    asyncio.run(scenario())
