import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from engine.adapters.sandbox.smolvm import SmolvmError, SmolvmSandbox
from engine.adapters.sandbox.smolvm import _guest, support
from engine.ports import SandboxSpec


@pytest.mark.parametrize("system,architecture,release,problem", [
    ("Darwin", "x86_64", "14.0", "Apple Silicon"),
    ("Darwin", "arm64", "10.15", "macOS 11"),
    ("Windows", "AMD64", "", "not Windows"),
    ("Linux", "riscv64", "", "architecture"),
])
def test_unsupported_hosts(system, architecture, release, problem, monkeypatch):
    monkeypatch.setattr(support.platform, "system", lambda: system)
    monkeypatch.setattr(support.platform, "machine", lambda: architecture)
    monkeypatch.setattr(support.platform, "mac_ver", lambda: (release, (), ""))
    result = support.detect_support()
    assert not result.available and problem in result.reason


@pytest.mark.parametrize("system,architecture", [("Darwin", "arm64"), ("Linux", "x86_64"), ("Linux", "aarch64")])
def test_supported_hosts(system, architecture, monkeypatch):
    monkeypatch.setattr(support.platform, "system", lambda: system)
    monkeypatch.setattr(support.platform, "machine", lambda: architecture)
    monkeypatch.setattr(support.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(support, "_kvm_problem", lambda: None)
    monkeypatch.setattr(support.shutil, "which", lambda *args, **kwargs: "/tools/smolvm")
    monkeypatch.setattr(support.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "smolvm 1.25.2", ""))
    assert support.detect_support().available


@pytest.mark.parametrize("version,problem", [(None, "not on PATH"), ("smolvm 1.24.0", "1.25.2"), ("unexpected", "unrecognized")])
def test_binary_diagnostics(version, problem, monkeypatch):
    monkeypatch.setattr(support.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(support.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(support.platform, "mac_ver", lambda: ("14.0", (), ""))
    monkeypatch.setattr(support.shutil, "which", lambda *args, **kwargs: "/tools/smolvm" if version else None)
    monkeypatch.setattr(support.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, version, ""))
    result = support.detect_support()
    assert not result.available and problem in result.reason


def test_linux_requires_accessible_kvm(monkeypatch):
    monkeypatch.setattr(support.platform, "system", lambda: "Linux")
    monkeypatch.setattr(support.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(support, "_kvm_problem", lambda: "KVM is unavailable: permission denied")
    assert "permission denied" in support.detect_support().reason


def test_guest_image_pins_match_provider_adapters():
    from langgraph_acp.providers.codex import CODEX_ACP_VERSION
    from langgraph_acp.providers.claude import CLAUDE_ACP_VERSION

    guest = Path(__file__).parents[1] / "packages/adapters/sandbox/smolvm/guest"
    dependencies = json.loads((guest / "package.json").read_text())["dependencies"]
    assert dependencies == {
        "@agentclientprotocol/codex-acp": CODEX_ACP_VERSION,
        "@agentclientprotocol/claude-agent-acp": CLAUDE_ACP_VERSION,
    }
    locked = json.loads((guest / "package-lock.json").read_text())["packages"]
    for package, version in dependencies.items():
        assert locked[f"node_modules/{package}"]["version"] == version


def test_secret_refs_and_host_environment_are_not_forwarded(smolvm_cli, monkeypatch):
    launcher, root = smolvm_cli
    monkeypatch.setenv("GITHUB_TOKEN", "host-forge-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unselected-provider-secret")
    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(
            SandboxSpec(secrets={"SELECTED_KEY": "selected-secret"}, labels={"run": "test-run"}),
        ) as sandbox:
            process = await sandbox.exec([sys.executable, "-c",
                "import os; print(os.getenv('GITHUB_TOKEN')); print(os.getenv('OPENAI_API_KEY')); print(os.getenv('SELECTED_KEY'))"],
                env={"SELECTED_KEY": "override"})
            assert await process.stdout.read() == b"None\nNone\nselected-secret\n"
            assert await process.wait() == 0
    asyncio.run(run())
    commands = (root / "commands.jsonl").read_text()
    assert "selected-secret" not in commands
    assert "host-forge-secret" not in commands
    assert "unselected-provider-secret" not in commands
    assert "run=test-run" in commands
    assert "--volume" not in commands and "--ssh-agent" not in commands
    assert not list(root.glob("oe-sandbox-*"))


def test_boot_failure_removes_machine(smolvm_cli):
    launcher, root = smolvm_cli
    (root / "fail-start").touch()
    async def run():
        with pytest.raises(SmolvmError, match="start failed"):
            async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()):
                pytest.fail("failed machine was yielded")
    asyncio.run(run())
    assert not list(root.glob("oe-sandbox-*"))


def test_failed_creation_preserves_original_error(smolvm_cli):
    launcher, root = smolvm_cli
    (root / "fail-create").touch()
    async def run():
        with pytest.raises(SmolvmError, match="create failed"):
            async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()):
                pytest.fail("failed machine was yielded")
    asyncio.run(run())
    assert not list(root.glob("oe-sandbox-*"))


def test_delete_failure_is_reported(smolvm_cli):
    launcher, root = smolvm_cli
    async def run():
        with pytest.raises(SmolvmError, match="could not delete"):
            async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()):
                (root / "fail-delete").touch()
    try:
        asyncio.run(run())
        assert list(root.glob("oe-sandbox-*"))
    finally:
        # The fake has no VMM process; remove its deliberately retained record.
        import shutil
        for path in root.glob("oe-sandbox-*"):
            shutil.rmtree(path)


def test_unsupported_policy_is_rejected_before_boot(smolvm_cli):
    launcher, root = smolvm_cli
    async def run():
        with pytest.raises(ValueError, match="egress"):
            async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec(egress_allowlist=())):
                pytest.fail("unsupported egress policy was ignored")
    asyncio.run(run())
    assert not (root / "commands.jsonl").exists()


@pytest.mark.parametrize("cancellations", [1, 3])
def test_repeated_cancellation_during_spawn_cleans_up(smolvm_cli, monkeypatch, cancellations):
    launcher, root = smolvm_cli
    original = asyncio.create_subprocess_exec

    async def run():
        spawned, release = asyncio.Event(), asyncio.Event()
        child = None
        async def delayed(*args, **kwargs):
            nonlocal child
            process = await original(*args, **kwargs)
            if "--workdir" in args:
                child = process
                spawned.set()
                await release.wait()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)

        async def body():
            async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
                await sandbox.exec([sys.executable, "-c", "import time; time.sleep(60)"])
        task = asyncio.create_task(body())
        try:
            await spawned.wait()
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert child.returncode is not None
            assert not list(root.glob("oe-sandbox-*"))
        finally:
            release.set()
            if child is not None and child.returncode is None:
                child.kill()
                await child.communicate()
    asyncio.run(asyncio.wait_for(run(), 15))


@pytest.mark.parametrize("destination", ["alias", "alias/data"])
@pytest.mark.parametrize("target_exists", [True, False])
def test_copy_in_rejects_symlink_destination(smolvm_cli, tmp_path, destination, target_exists):
    launcher, root = smolvm_cli
    source = tmp_path / "replacement"
    source.write_text("replacement")

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            workspace = root / sandbox.name / "workspace"
            target = workspace / "target"
            if target_exists:
                target.mkdir()
                (target / "data").write_text("original")
            (workspace / "alias").symlink_to("target", target_is_directory=True)
            with pytest.raises(ValueError, match="symlink"):
                await sandbox.copy_in(source, destination)
            assert (workspace / "alias").is_symlink()
            if target_exists:
                assert list(target.iterdir()) == [target / "data"]
                assert (target / "data").read_text() == "original"
            else:
                assert not target.exists()
    asyncio.run(run())


def test_copy_out_rejects_symlink_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("replacement")
    archive = tmp_path / "transfer.tar"
    _guest.pack(source, archive)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("original")
    destination = tmp_path / "destination"
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        _guest.unpack(archive, destination)
    assert (outside / "data").read_text() == "original"


def test_copy_out_rejects_traversal_archive(tmp_path):
    archive = tmp_path / "transfer.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("../escaped")
        output.addfile(member)
    with pytest.raises(tarfile.FilterError):
        _guest.unpack(archive, tmp_path / "destination")
    assert not (tmp_path / "escaped").exists()
