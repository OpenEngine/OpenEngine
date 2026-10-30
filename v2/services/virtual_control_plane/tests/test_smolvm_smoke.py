"""Opt-in: create, restart and ensure one real smolvm machine through the lifecycle.

Set `OE_VCP_SMOLVM_IMAGE` to a guest image with `sh` and `tar` (and `git` if
`OE_VCP_SMOLVM_REPOSITORY` names a repository to clone) on a host with the
`smolvm` CLI and virtualization, for example:

    OE_VCP_SMOLVM_IMAGE=alpine:3.20 uv run --project v2 --frozen pytest \\
        v2/services/virtual_control_plane/tests/test_smolvm_smoke.py -s

Routine CI leaves this skipped and relies on the deterministic CLI fake. The
test deletes the machine it created, including after a failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from openengine.messaging import Request, envelopes
from openengine.persistence import DatabaseConfig

from virtual_control_plane.application.lifecycle import Lifecycle
from virtual_control_plane.application.ports import Ownership
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.providers.smolvm import Smolvm, machine_name

IMAGE = os.environ.get("OE_VCP_SMOLVM_IMAGE")
EXECUTABLE = os.environ.get("OE_VCP_SMOLVM_EXECUTABLE", "smolvm")
REPOSITORY = os.environ.get("OE_VCP_SMOLVM_REPOSITORY")
DEADLINE_SECONDS = float(os.environ.get("OE_VCP_SMOLVM_SMOKE_DEADLINE", "900"))

pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which(EXECUTABLE) is None,
    reason="real smolvm smoke test: set OE_VCP_SMOLVM_IMAGE with the smolvm CLI installed",
)


def _cli(*arguments: str) -> str:
    return subprocess.run([EXECUTABLE, "machine", *arguments], check=True, capture_output=True, text=True).stdout


def _request(subject: str, payload: dict, parameters: dict | None = None) -> Request:
    return Request(subject, "control", subject.removeprefix("oe.v1.vcp.control."), envelopes.command(payload),
                   "nats://127.0.0.1:4222", parameters or {})


async def _finish(lifecycle: Lifecycle, store: DurableStore, operation_id: str) -> dict:
    """Reconcile until the operation is terminal; transient retries are part of the real path."""
    async with asyncio.timeout(DEADLINE_SECONDS):
        while True:
            await lifecycle.reconcile()
            operation = (await store.read("operations", operation_id))["snapshot"]
            if operation["status"] in ("succeeded", "failed"):
                return operation
            await asyncio.sleep(1)


def test_real_smolvm_create_restart_and_ensure(tmp_path: Path) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    context: dict = {"instructions": "Smoke test instructions.\n",
                     "files": [{"path": "notes/hello.txt", "content": "hello from the host\n"}]}
    tools = ["terminal"]
    if REPOSITORY:
        context["repositories"] = [{"url": REPOSITORY}]
        tools.append("git")
    document = {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": "smoke"},
                "spec": {"provider": "smolvm", "tools": tools, "resources": {"cpuMillis": 1000, "memoryMib": 1024},
                         "context": context}}
    machine: str | None = None

    async def scenario() -> None:
        nonlocal machine
        store = DurableStore(PERSISTENCE.database(config))
        lifecycle = Lifecycle(store, {"smolvm": Smolvm(IMAGE, tools=tools, executable=EXECUTABLE)})
        created = await lifecycle.apply(_request("oe.v1.vcp.control.workspaces.apply", {"workspace": document}))
        assert created["status"] == "accepted", created
        workspace_id = created["resource_id"]
        runtime = await store.read("workspace_runtimes", workspace_id)
        machine = machine_name(Ownership(workspace_id, runtime["runtime_id"], runtime["generation"]))
        operation = await _finish(lifecycle, store, created["operation_id"])
        assert operation["status"] == "succeeded", operation
        runtime = await store.read("workspace_runtimes", workspace_id)
        assert runtime["locator"]["machine"] == machine
        ready = (await store.read("workspaces", workspace_id))["snapshot"]
        assert ready["state"] == "ready"
        [listed] = [entry for entry in json.loads(_cli("ls", "--json")) if entry["name"] == machine]
        assert listed["labels"]["oe.workspace"] == workspace_id and listed["labels"]["oe.runtime"] == runtime["runtime_id"]
        assert _cli("exec", "--name", machine, "--", "cat", "/workspace/AGENTS.md") == context["instructions"]
        assert _cli("exec", "--name", machine, "--", "cat", "/workspace/notes/hello.txt") == "hello from the host\n"
        if REPOSITORY:
            assert ".git" in _cli("exec", "--name", machine, "--", "ls", "-a", "/workspace/repositories/0")
        await store.close()

        # Restart: a fresh store, lifecycle and adapter must reassert ownership, not reprovision.
        store = DurableStore(PERSISTENCE.database(config))
        lifecycle = Lifecycle(store, {"smolvm": Smolvm(IMAGE, tools=tools, executable=EXECUTABLE)})
        ensured = await lifecycle.ensure(_request(f"oe.v1.vcp.control.workspaces.{workspace_id}.ensure", {},
                                                  {"workspace_id": workspace_id}))
        assert ensured["status"] == "accepted", ensured
        operation = await _finish(lifecycle, store, ensured["operation_id"])
        assert operation["status"] == "succeeded", operation
        assert (await store.read("workspaces", workspace_id))["snapshot"] == ready
        owned = [entry for entry in json.loads(_cli("ls", "--json")) if (entry.get("labels") or {}).get("oe.workspace") == workspace_id]
        assert len(owned) == 1
        await store.close()

    try:
        asyncio.run(scenario())
    finally:
        if machine:
            subprocess.run([EXECUTABLE, "machine", "delete", "--name", machine, "--force"], check=False)
