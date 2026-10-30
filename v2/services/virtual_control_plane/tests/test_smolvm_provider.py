"""The smolvm adapter against the deterministic CLI fake: ownership, idempotent
provisioning, resumable preparation, honest capabilities and failure classes.
No virtualization is needed; real `sh`, `git` and `tar` act as the guest."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest
from openengine.messaging import ServiceError, envelopes

from virtual_control_plane.application.ports import Ownership, OwnershipMismatch, RuntimeContext
from virtual_control_plane.providers.smolvm import GUEST_ROOT, Smolvm, machine_name
from virtual_control_plane.tests import smolvm_fake

OWNERSHIP = Ownership("ws_a", "rt_a", 1)


def document(**spec) -> dict:
    return {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": "example"},
            "spec": {"provider": "smolvm", **spec}}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "smolvm"


@pytest.fixture
def adapter(root: Path) -> Smolvm:
    return Smolvm("example.test/python-git:1", executable=str(smolvm_fake.install(root)))


@pytest.fixture
def origin(tmp_path: Path) -> dict[str, str]:
    """A local repository with a `main` commit and a `feature` branch to check out."""
    path = tmp_path / "origin"
    path.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-C", str(path)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (path / "README").write_text("main\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "main"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "feature"], check=True)
    (path / "README").write_text("feature\n")
    subprocess.run([*git, "commit", "-q", "-am", "feature"], check=True)
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    return {"url": str(path), "ref": "feature"}


def subcommands(root: Path) -> list[str]:
    return [command[1] for command in smolvm_fake.commands(root)]


def test_capabilities_report_microvm_isolation_and_the_configured_tools_only(root: Path) -> None:
    adapter = Smolvm("img", tools=("terminal", "browser", "terminal"), executable=str(smolvm_fake.install(root)))

    capabilities = adapter.capabilities

    envelopes.check("virtual_control_plane/v1/schemas/capabilities.schema.json#/$defs/provider_capabilities", capabilities)
    assert capabilities["provider"] == "smolvm" and capabilities["isolation"] == "microvm"
    assert capabilities["tools"] == ["terminal", "browser"]
    assert (capabilities["checkpoint"], capabilities["suspend"], capabilities["restore"]) == (False, False, "none")
    assert smolvm_fake.commands(root) == []  # reporting never runs the CLI


def test_validate_rejects_fractional_vcpus_and_context_paths_outside_the_workspace(adapter: Smolvm) -> None:
    adapter.validate(document(resources={"cpuMillis": 2000}, context={"files": [{"path": "a/b.md", "content": ""}]}))

    with pytest.raises(ServiceError) as error:
        adapter.validate(document(resources={"cpuMillis": 1500}, context={"files": [
            {"path": "../escape", "content": ""}, {"path": "/abs", "content": ""}, {"path": "ok", "content": ""}]}))

    assert error.value.code == "INVALID_ARGUMENT"
    assert [violation["field"] for violation in error.value.details["violations"]] == [
        "payload.workspace.spec.resources.cpuMillis",
        "payload.workspace.spec.context.files[0].path",
        "payload.workspace.spec.context.files[1].path",
    ]


def test_provision_creates_an_owned_labelled_machine_prepares_it_and_is_idempotent(
    adapter: Smolvm, root: Path, origin: dict[str, str],
) -> None:
    specification = document(
        resources={"cpuMillis": 2000, "memoryMib": 1024, "diskGib": 10}, tools=["git", "terminal"],
        context={"repositories": [origin], "files": [{"path": "notes/todo.md", "content": "write tests\n"}],
                 "instructions": "Be careful.\n"},
    )

    locator = asyncio.run(adapter.provision(OWNERSHIP, specification))

    name = machine_name(OWNERSHIP)
    machine = smolvm_fake.machines(root)[name]
    assert name == "oe-vcp-rt_a"
    assert machine["labels"] == {"oe.workspace": "ws_a", "oe.runtime": "rt_a", "oe.generation": "1"}
    assert (machine["image"], machine["cpus"], machine["memory_mib"], machine["storage_gb"]) == (adapter.image, 2, 1024, 10)
    assert machine["network"] and machine["state"] == "running" and machine["cmd"] == ["sleep", "infinity"]
    assert locator == {"machine": name, "image": adapter.image, "labels": machine["labels"], "root": GUEST_ROOT}
    guest = smolvm_fake.guest(root, name)
    assert (guest / "workspace/repositories/0/README").read_text() == "feature\n"
    assert (guest / "workspace/notes/todo.md").read_text() == "write tests\n"
    assert (guest / "workspace/AGENTS.md").read_text() == "Be careful.\n"
    assert (guest / "oe-vcp/ready").read_text() == "rt_a"
    assert not (guest / "oe-vcp/context.tar").exists()
    assert adapter.context(OWNERSHIP, locator, specification) == RuntimeContext(
        root="/workspace", working_directory="repositories/0", instruction_files=("AGENTS.md",))

    before = len(smolvm_fake.commands(root))
    assert asyncio.run(adapter.provision(OWNERSHIP, specification)) == locator

    # Already owned, running and prepared: look, check, read the marker, stop.
    assert subcommands(root)[before:] == ["ls", "status", "exec"]


def test_interrupted_preparation_resumes_without_recloning_or_recreating(
    adapter: Smolvm, root: Path, origin: dict[str, str],
) -> None:
    specification = document(context={"repositories": [origin], "files": [{"path": "f", "content": "x"}]})
    smolvm_fake.inject_faults(root, cp=1)  # the context transfer fails once, after the clone

    with pytest.raises(ServiceError) as error:
        asyncio.run(adapter.provision(OWNERSHIP, specification))

    assert error.value.code == "UNAVAILABLE" and error.value.retryable
    guest = smolvm_fake.guest(root, machine_name(OWNERSHIP))
    assert (guest / "workspace/repositories/0/.git").is_dir() and not (guest / "oe-vcp/ready").exists()
    (guest / "workspace/repositories/0/sentinel").write_text("kept")

    asyncio.run(adapter.provision(OWNERSHIP, specification))

    assert (guest / "oe-vcp/ready").read_text() == "rt_a"
    assert (guest / "workspace/f").read_text() == "x"
    assert (guest / "workspace/repositories/0/sentinel").read_text() == "kept"
    assert subcommands(root).count("create") == 1


def test_a_foreign_machine_under_our_name_is_refused_and_left_untouched(adapter: Smolvm, root: Path) -> None:
    foreign = {"name": machine_name(OWNERSHIP), "labels": {"oe.workspace": "ws_other", "oe.runtime": "rt_a"},
               "image": "theirs", "state": "created", "cpus": 1, "memory_mib": 256, "storage_gb": None,
               "network": False, "cmd": []}
    smolvm_fake.save_machine(root, foreign)

    with pytest.raises(OwnershipMismatch):
        asyncio.run(adapter.provision(OWNERSHIP, document()))
    with pytest.raises(OwnershipMismatch):
        asyncio.run(adapter.ensure(OWNERSHIP, None, document()))

    assert subcommands(root) == ["ls", "ls"]  # looked, never created, started, prepared or deleted
    assert smolvm_fake.machines(root)[foreign["name"]] == foreign


def test_ensure_restarts_a_stopped_machine_and_refuses_to_reprovision_a_vanished_one(
    adapter: Smolvm, root: Path,
) -> None:
    specification = document(context={"instructions": "hi"})
    locator = asyncio.run(adapter.provision(OWNERSHIP, specification))
    name = machine_name(OWNERSHIP)
    smolvm_fake.save_machine(root, {**smolvm_fake.machines(root)[name], "state": "stopped"})
    before = len(smolvm_fake.commands(root))

    assert asyncio.run(adapter.ensure(OWNERSHIP, locator, specification)) == locator

    assert smolvm_fake.machines(root)[name]["state"] == "running"
    assert subcommands(root)[before:] == ["ls", "status", "start", "exec"]  # prepared already: marker read only

    with pytest.raises(ServiceError, match="not the owned"):
        asyncio.run(adapter.ensure(OWNERSHIP, {**locator, "machine": "oe-vcp-rt_other"}, specification))
    (root / "machines" / f"{name}.json").unlink()
    with pytest.raises(ServiceError) as error:
        asyncio.run(adapter.ensure(OWNERSHIP, locator, specification))

    assert error.value.code == "PROVIDER_ERROR" and not error.value.retryable
    assert error.value.details == {"machine": name}
    assert smolvm_fake.machines(root) == {} and "create" not in subcommands(root)[before:]


def test_a_tool_missing_from_the_image_fails_preparation_terminally(root: Path) -> None:
    adapter = Smolvm("img", tools=("terminal", "oe-missing-tool"), executable=str(smolvm_fake.install(root)))

    with pytest.raises(ServiceError) as error:
        asyncio.run(adapter.provision(OWNERSHIP, document(tools=["terminal", "oe-missing-tool"])))

    assert error.value.code == "PROVIDER_ERROR" and not error.value.retryable
    assert error.value.details == {"tool": "oe-missing-tool", "image": "img"}
    assert not (smolvm_fake.guest(root, machine_name(OWNERSHIP)) / "oe-vcp/ready").exists()


def test_cli_failures_are_transient_while_a_missing_or_hung_cli_is_handled(adapter: Smolvm, root: Path, tmp_path: Path) -> None:
    smolvm_fake.inject_faults(root, start=1)
    with pytest.raises(ServiceError) as error:
        asyncio.run(adapter.provision(OWNERSHIP, document()))
    assert error.value.code == "UNAVAILABLE" and error.value.retryable
    assert error.value.details == {"command": "machine start"}
    asyncio.run(adapter.provision(OWNERSHIP, document()))  # the retry converges on the same machine
    assert len(smolvm_fake.machines(root)) == 1

    absent = Smolvm("img", executable=str(tmp_path / "no-such-smolvm"))
    with pytest.raises(ServiceError) as error:
        asyncio.run(absent.provision(OWNERSHIP, document()))
    assert error.value.code == "PROVIDER_ERROR" and not error.value.retryable

    hung = tmp_path / "hung-smolvm"
    hung.write_text("#!/bin/sh\nsleep 30\n")
    hung.chmod(0o755)
    slow = Smolvm("img", executable=str(hung), command_timeout=0.2)
    started = time.monotonic()
    with pytest.raises(ServiceError, match="timed out"):
        asyncio.run(slow.provision(OWNERSHIP, document()))
    assert time.monotonic() - started < 5
    assert json.loads(json.dumps(error.value.to_dict()))["code"] == "PROVIDER_ERROR"
