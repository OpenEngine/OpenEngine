"""Build one native, offline release from uv.lock and the CI-built frontend.

Run with the pinned release Python and uv on the target platform. All compilation
happens here; the installer accepts only the resulting, hash-pinned wheels.
"""

import argparse
import email
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

PYTHON = "3.12.12"
UV = "0.12.7"
ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", args.version):
        parser.error("version must be a release version, e.g. 1.0.0 or 1.0.0rc1")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("commit must be the full source SHA")
    assert platform.python_version() == PYTHON
    assert subprocess.check_output(["uv", "--version"], text=True).split()[1] == UV
    target = f"{platform.system().lower()}-{platform.machine().lower()}"
    assert target in {"linux-x86_64", "darwin-arm64"}, target
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("output must be outside the source tree")
    output.mkdir(parents=True, exist_ok=False)
    wheels = output / "wheels"
    wheels.mkdir()
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary) / "source"
        shutil.copytree(ROOT, stage, ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "release-output"))
        requirements = Path(temporary) / "third-party.txt"
        run("uv", "export", "--frozen", "--all-packages", "--no-dev",
            "--no-emit-workspace", "--no-emit-package", "langgraph-acp",
            "--no-editable", "--output-file", str(requirements), cwd=stage, stdout=subprocess.DEVNULL)
        # pip is only a CI wheel downloader; never shipped or needed by install.sh.
        run("uv", "run", "--no-project", "--python", sys.executable, "--with", "pip==25.3", "python", "-m", "pip",
            "download", "--only-binary=:all:", "--no-deps", "--require-hashes",
            "-r", str(requirements), "-d", str(wheels))
        for project in stage.rglob("pyproject.toml"):
            project.write_text(re.sub(r'^version = "[^"]+"',
                f'version = "{args.version}"', project.read_text(), count=1, flags=re.M))
        run("uv", "build", "--all-packages", "--wheel", "--no-sources",
            "--out-dir", str(wheels), cwd=stage)
        run("uv", "build", "langgraph-acp", "--wheel", "--no-sources",
            "--out-dir", str(wheels), cwd=stage)
        runtime = Path(temporary) / "runtime"
        run("uv", "python", "install", PYTHON, "--install-dir", str(runtime),
            env={**os.environ, "UV_PYTHON_BIN_DIR": str(Path(temporary) / "bin")})
        python = next(runtime.glob("cpython-*"))
        shutil.copytree(python, output / "python", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(shutil.which("uv"), output / "uv")
    shutil.copy2(ROOT / "release/install.sh", output / "install.sh")
    dependencies = []
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            metadata = email.message_from_bytes(archive.read(next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))))
        dependencies.append({"name": metadata["Name"], "version": metadata["Version"],
                             "artifact": f"wheels/{wheel.name}", "sha256": digest(wheel)})
    (output / "requirements.txt").write_text("".join(
        f'{dep["name"]}=={dep["version"]} --hash=sha256:{dep["sha256"]}\n' for dep in dependencies))
    manifest = {"schema_version": 1, "version": args.version, "source_commit": args.commit,
                "platform": target, "tested_architecture": platform.machine(),
                "platform_baseline": "Ubuntu 24.04 (glibc)" if target.startswith("linux") else "macOS 14",
                "python": PYTHON, "dependencies": dependencies,
                "runtime_components": [
                    {"name": "uv", "version": UV, "provisioning": "bundled executable"},
                    {"name": "cpython", "version": PYTHON,
                     "provisioning": "uv-managed standalone runtime, bundled in python/"}],
                "startup": "engine-web", "artifacts": {}}
    manifest["artifacts"] = {str(p.relative_to(output)): digest(p)
                             for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(
        f"{digest(p)}  {p.relative_to(output)}\n"
        for p in sorted(output.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"))


if __name__ == "__main__":
    main()
