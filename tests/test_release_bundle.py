"""An installed release runs with no source checkout anywhere in sight."""

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _build_release(output: Path) -> Path:
    spec = importlib.util.spec_from_file_location(
        "build_release", ROOT / "scripts" / "build_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build("test", output)


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("uv") is None
    or shutil.which("npm") is None
    or not (ROOT / "apps" / "web" / "node_modules").is_dir(),
    reason="building the bundle needs uv, npm, and the web client's node_modules",
)
def test_installed_bundle_serves_health_from_outside_a_checkout(tmp_path: Path) -> None:
    archive = _build_release(tmp_path / "dist")
    with tarfile.open(archive) as bundle_archive:
        bundle_archive.extractall(tmp_path / "release", filter="data")
    (bundle,) = (tmp_path / "release").iterdir()

    manifest = json.loads((bundle / "release-manifest.json").read_text())
    published = json.loads((archive.parent / "release-manifest.json").read_text())
    assert published.pop("archive_sha256") == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert published == manifest
    assert manifest["requirements"] == "requirements.txt"
    assert manifest["config"] == "engine.toml"
    recorded = {entry["path"] for entry in manifest["files"]}
    assert {"requirements.txt", "engine.toml"} <= recorded
    requirements = (bundle / "requirements.txt").read_text()
    assert "--hash=sha256:" in requirements
    assert "-e " not in requirements
    for wheel in (bundle / "wheels").glob("*.whl"):
        assert f"./wheels/{wheel.name}" in requirements

    # The deployment: the bundle's config and workflows, nothing else.
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    shutil.copy2(bundle / "engine.toml", deployment / "engine.toml")
    shutil.copytree(bundle / "workflows", deployment / "workflows")
    config = deployment / "engine.toml"

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("ENGINE_", "VIRTUAL_ENV", "PYTHON", "UV_PROJECT"))
    }
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    environment["TMPDIR"] = str(temporary)
    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        cwd=tmp_path, env=environment, check=True,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--require-hashes",
         "-r", "requirements.txt"],
        cwd=bundle, env=environment, check=True,
    )

    port = _unused_port()
    environment["ENGINE_PORT"] = str(port)
    working_directory = tmp_path / "anywhere"
    working_directory.mkdir()
    log = tmp_path / "engine-web.log"
    with log.open("wb") as output:
        server = subprocess.Popen(
            [str(python.with_name("engine-web")), "--config", str(config)],
            cwd=working_directory, env=environment, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            status = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=2
                    ) as response:
                        status = response.status
                        break
                except OSError:
                    time.sleep(0.5)
            assert status == 200, log.read_text()
        finally:
            server.terminate()
            server.wait(timeout=30)

    state = deployment / "state"
    assert (state / "conversations.sqlite3").is_file()
    assert (state / "graph-state").is_dir()
    databases = {
        path
        for path in tmp_path.rglob("*")
        if path.suffix in {".sqlite3", ".db", ".sqlite"} and venv not in path.parents
    }
    assert databases and all(state in path.parents for path in databases), databases
    assert not any(working_directory.iterdir())
    # temporalio downloads its server into the temporary directory.
    temporal = [
        path
        for directory in (temporary, deployment, working_directory)
        for path in directory.rglob("*temporal*")
    ]
    assert not temporal, temporal
    assert "temporal" not in log.read_text().lower()
