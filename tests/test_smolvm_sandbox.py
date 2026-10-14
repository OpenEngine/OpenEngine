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


@pytest.mark.parametrize("target", ["..", "../outside"])
def test_transfer_rejects_links_outside_payload(tmp_path, target):
    source = tmp_path / "source"
    source.mkdir()
    (source / "up").symlink_to(target)
    archive = tmp_path / "transfer.tar"
    _guest.pack(source, archive)
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="copied tree"):
        _guest.unpack(archive, destination)
    assert not destination.exists()


def test_transfer_rejects_links_through_existing_host_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to("existing")
    archive = tmp_path / "transfer.tar"
    _guest.pack(source, archive)
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "existing").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="copied tree"):
        _guest.unpack(archive, destination)
    assert not (destination / "link").exists()


def test_copy_out_rejects_guest_link_to_host_parent(smolvm_cli, tmp_path):
    launcher, root = smolvm_cli
    destination = tmp_path / "output"

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            workspace = root / sandbox.name / "workspace"
            (workspace / "up").symlink_to("..", target_is_directory=True)
            with pytest.raises(ValueError, match="copied tree"):
                await sandbox.copy_out(".", destination)
    asyncio.run(run())
    assert not destination.exists()


def test_transfer_preserves_internal_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("safe")
    (source / "nested").mkdir()
    (source / "nested/link").symlink_to("../data")
    (source / "alias").symlink_to("nested/link")
    archive = tmp_path / "transfer.tar"
    _guest.pack(source, archive)
    destination = tmp_path / "destination"
    _guest.unpack(archive, destination)
    assert (destination / "nested/link").readlink() == Path("../data")
    assert (destination / "alias").readlink() == Path("nested/link")
    assert (destination / "alias").read_text() == "safe"


@pytest.mark.parametrize("basename", ["../outside", "/outside", "..", "nested\\outside"])
def test_transfer_rejects_untrusted_basename(tmp_path, basename):
    archive = tmp_path / "transfer.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("payload")
        member.pax_headers["openengine.basename"] = basename
        output.addfile(member)
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(ValueError, match="basename"):
        _guest.unpack(archive, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("budget", ["archive", "bytes", "members", "hardlink"])
def test_unpack_limits_before_destination_copy(tmp_path, monkeypatch, budget):
    import io

    archive = tmp_path / "input.tar"
    with tarfile.open(archive, "w") as output:
        root = tarfile.TarInfo("payload")
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        for index in range(2):
            member = tarfile.TarInfo(f"payload/file{index}")
            member.size = 4
            output.addfile(member, io.BytesIO(b"data"))
        if budget == "hardlink":
            member = tarfile.TarInfo("payload/link")
            member.type = tarfile.LNKTYPE
            member.linkname = "payload/file0"
            output.addfile(member)
    if budget == "archive":
        monkeypatch.setattr(_guest, "MAX_ARCHIVE_BYTES", archive.stat().st_size - 1)
    elif budget == "bytes":
        monkeypatch.setattr(_guest, "MAX_EXTRACTED_BYTES", 7)
    elif budget == "members":
        monkeypatch.setattr(_guest, "MAX_MEMBERS", 2)
    monkeypatch.setattr(_guest.tempfile, "tempdir", str(tmp_path))
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="budget|hard links"):
        _guest.unpack(archive, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob("oe-transfer-*"))


def test_copy_out_bounds_untrusted_stream_and_cleans_up(smolvm_cli, tmp_path, monkeypatch):
    launcher, root = smolvm_cli
    monkeypatch.setattr(_guest, "MAX_ARCHIVE_BYTES", 10240)
    destination = tmp_path / "output"
    monkeypatch.setattr(_guest.tempfile, "tempdir", str(tmp_path))

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            workspace = root / sandbox.name / "workspace"
            (workspace / "large").write_bytes(b"x" * 65536)
            with pytest.raises(ValueError, match="archive exceeds byte budget"):
                await asyncio.wait_for(sandbox.copy_out(".", destination), 10)
            assert not list((root / sandbox.name).glob("oe-transfer-*"))
            assert not list(tmp_path.glob("oe-transfer-*"))
    asyncio.run(run())
    assert not destination.exists()


@pytest.mark.parametrize("direction", ["in", "out"])
@pytest.mark.parametrize("failure", ["value", "missing", "cancel", None])
def test_transfer_preserves_primary_error_when_cleanup_fails(smolvm_cli, tmp_path, monkeypatch, direction, failure):
    launcher, root = smolvm_cli
    source = tmp_path / "source"
    source.write_text("input")
    primary = {"value": ValueError("unsafe link"), "missing": FileNotFoundError("missing input"),
               "cancel": asyncio.CancelledError("cancelled transfer"), None: None}[failure]

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            (root / sandbox.name / "workspace/input").write_text("input")
            helper = sandbox._helper

            async def fail(operation, *arguments, **kwargs):
                if operation == "remove":
                    message = "cleanup unavailable"
                    raise SmolvmError(message)
                if primary is not None and operation == ("unpack" if direction == "in" else "pack"):
                    raise primary
                return await helper(operation, *arguments, **kwargs)

            monkeypatch.setattr(sandbox, "_helper", fail)
            expected = type(primary) if primary is not None else SmolvmError
            with pytest.raises(expected) as caught:
                if direction == "in":
                    await sandbox.copy_in(source, "input")
                else:
                    await sandbox.copy_out("input", tmp_path / "output")
            if primary is not None:
                assert caught.value is primary
                assert "cleanup unavailable" in primary.__notes__[0]
            else:
                assert "cleanup unavailable" in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize("version,allowed", [
    ((3, 11, 8), False), ((3, 11, 12), False), ((3, 11, 13), True),
    ((3, 12, 0), False), ((3, 12, 10), False), ((3, 12, 11), True),
    ((3, 13, 0), False), ((3, 13, 3), False), ((3, 13, 4), True),
    ((3, 14, 0), True),
])
def test_transfer_python_floor_and_package_metadata(monkeypatch, version, allowed):
    import tomllib
    from packaging.specifiers import SpecifierSet

    metadata = Path(__file__).parents[1] / "packages/adapters/sandbox/smolvm/pyproject.toml"
    requirement = SpecifierSet(tomllib.loads(metadata.read_text())["project"]["requires-python"])
    assert requirement.contains(".".join(map(str, version))) == allowed
    monkeypatch.setattr(_guest.sys, "version_info", version)
    if allowed:
        _guest.require_patched_python()
    else:
        with pytest.raises(RuntimeError, match="patched Python"):
            # Version validation precedes opening any guest-controlled archive.
            _guest.unpack(Path("nonexistent.tar"), Path("destination"))


@pytest.mark.parametrize("stream", ["stdout", "stderr", "both"])
def test_command_output_flood_is_bounded_and_process_reaped(monkeypatch, stream):
    import engine.adapters.sandbox.smolvm as adapter

    monkeypatch.setattr(adapter, "MAX_COMMAND_OUTPUT_BYTES", 1024)
    writes = "os.write(1, b'x' * 700); os.write(2, b'y' * 700)" if stream == "both" else (
        f"os.write({1 if stream == 'stdout' else 2}, b'x' * 2048)"
    )

    async def run():
        instance = adapter.SmolvmInstance(sys.executable, "fixture", SandboxSpec())
        with pytest.raises(SmolvmError, match="output exceeds byte budget"):
            await asyncio.wait_for(instance._command(["-c", f"import os, time; {writes}; time.sleep(60)"]), 5)
        assert not instance._processes
    asyncio.run(run())


def test_replaced_guest_helper_cannot_flood_host(smolvm_cli, monkeypatch):
    import engine.adapters.sandbox.smolvm as adapter

    launcher, root = smolvm_cli
    monkeypatch.setattr(adapter, "MAX_COMMAND_OUTPUT_BYTES", 1024)

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            helper = root / sandbox.name / "oe-sandbox-transfer.py"
            helper.write_text("import os, time\nos.write(1, b'x' * 65536)\ntime.sleep(60)\n")
            with pytest.raises(SmolvmError, match="output exceeds byte budget"):
                await asyncio.wait_for(sandbox._helper("init"), 5)
            assert not sandbox._processes
        assert not (root / sandbox.name).exists()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_copy_out_leaves_existing_host_destination_untouched(smolvm_cli, tmp_path, kind):
    launcher, root = smolvm_cli
    destination = tmp_path / "output"
    if kind == "directory":
        destination.mkdir()
        original = destination / "keep.txt"
    else:
        original = destination
    original.write_text("host data")
    original.chmod(0o600)

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            (root / sandbox.name / "workspace/keep.txt").write_text("guest replacement")
            with pytest.raises(ValueError, match="fresh path or an empty staging directory"):
                await sandbox.copy_out("." if kind == "directory" else "keep.txt", destination)
            assert not list((root / sandbox.name).glob("oe-transfer-*"))
    asyncio.run(run())
    assert original.read_text() == "host data"
    assert original.stat().st_mode & 0o777 == 0o600
    if kind == "directory":
        assert list(destination.iterdir()) == [original]


@pytest.mark.parametrize("git_name,source", [(".git", "."), (".GiT", "."), (".git", ".git")])
def test_copy_out_rejects_git_content_before_host_copy(smolvm_cli, tmp_path, git_name, source):
    launcher, root = smolvm_cli
    destination = tmp_path / "staging"
    destination.mkdir()

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            workspace = root / sandbox.name / "workspace"
            metadata = workspace / git_name
            (metadata / "hooks").mkdir(parents=True)
            hook = metadata / "hooks/post-checkout"
            hook.write_text("#!/bin/sh\necho unsafe\n")
            hook.chmod(0o755)
            (metadata / "config").write_text("[core]\nfsmonitor = echo unsafe\n")
            (workspace / "result.txt").write_text("ordinary output")
            with pytest.raises(ValueError, match="Git metadata"):
                await sandbox.copy_out(source, destination)
            assert not list((root / sandbox.name).glob("oe-transfer-*"))
    asyncio.run(run())
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("git_name", [".git", ".GIT"])
def test_copy_out_rejects_host_git_destination(smolvm_cli, tmp_path, git_name):
    launcher, root = smolvm_cli
    destination = tmp_path / "checkout" / git_name / "hooks/post-checkout"
    destination.parent.mkdir(parents=True)
    destination.write_text("host hook")
    destination.chmod(0o755)

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(SandboxSpec()) as sandbox:
            (root / sandbox.name / "workspace/result").write_text("guest hook")
            with pytest.raises(ValueError, match="Git metadata"):
                await sandbox.copy_out("result", destination)
    asyncio.run(run())
    assert destination.read_text() == "host hook"
    assert destination.stat().st_mode & 0o777 == 0o755


def test_copy_out_rejects_git_file_basename(tmp_path):
    source = tmp_path / ".git"
    source.write_text("gitdir: /outside")
    archive = tmp_path / "transfer.tar"
    _guest.pack(source, archive)
    destination = tmp_path / "staging"
    with pytest.raises(ValueError, match="Git metadata"):
        _guest.unpack(archive, destination, True)
    assert not destination.exists()


@pytest.mark.parametrize("kind,expected", [
    ("FileNotFoundError", FileNotFoundError), ("ValueError", ValueError),
    ("FilterError", ValueError), ("OutsideDestinationError", ValueError),
    ("AbsoluteLinkError", ValueError), ("LinkOutsideDestinationError", ValueError),
    ("RuntimeError", SmolvmError),
])
def test_guest_error_json_redacts_secrets_before_host_exception(smolvm_cli, kind, expected):
    launcher, root = smolvm_cli

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(
            SandboxSpec(secrets={"SHORT": "token", "LONG": "token-sensitive", "OTHER": "private-value", "EMPTY": ""})
        ) as sandbox:
            helper = root / sandbox.name / "oe-sandbox-transfer.py"
            report = json.dumps({"error": kind, "message": "failed: token-sensitive; private-value; token"})
            helper.write_text(f"import sys\nprint({report!r})\nsys.exit(2)\n")
            with pytest.raises(expected) as caught:
                await sandbox._helper("check", ".", "python")
            assert str(caught.value) == "failed: [redacted]; [redacted]; [redacted]"
            assert "sensitive" not in repr(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize("report", [[], "private-value", {"error": [], "message": ["private-value"]}])
def test_malformed_guest_error_json_does_not_leak_secrets(smolvm_cli, report):
    launcher, root = smolvm_cli

    async def run():
        async with SmolvmSandbox("fixture", executable=str(launcher)).create(
            SandboxSpec(secrets={"KEY": "private-value"})
        ) as sandbox:
            helper = root / sandbox.name / "oe-sandbox-transfer.py"
            helper.write_text(f"import sys\nprint({json.dumps(report)!r})\nsys.exit(2)\n")
            with pytest.raises(SmolvmError, match="guest transfer failed") as caught:
                await sandbox._helper("init")
            assert "private-value" not in repr(caught.value)
    asyncio.run(run())
