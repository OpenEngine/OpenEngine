"""Build a native, offline release from the frozen workspace lock (CI only)."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from email.parser import BytesParser

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = json.loads((ROOT / "release/versions.json").read_text())


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
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.+-]*)", args.version):
        parser.error("version must start with MAJOR.MINOR.PATCH")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("commit must be a full source SHA")
    target = f"{platform.system()}-{platform.machine()}"
    if target not in VERSIONS["platforms"]:
        parser.error(f"unsupported build host: {target}")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    wheels = out / "wheels"
    wheels.mkdir()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        exported = work / "export.txt"
        run("uv", "export", "--frozen", "--package", "engine-web", "--no-dev",
            "--no-editable", "--no-hashes", "-o", str(exported), cwd=ROOT,
            stdout=subprocess.DEVNULL)
        lines = exported.read_text().splitlines()
        locals_ = [line for line in lines if line.startswith(".")]
        dependencies = work / "dependencies.txt"
        dependencies.write_text("\n".join(line for line in lines if not line.startswith(".")))
        # Stage sources so release version stamping never modifies the checkout.
        stage = work / "source"
        for local in locals_:
            source = ROOT / local
            destination = stage / local
            destination.mkdir(parents=True, exist_ok=True)
            project = (source / "pyproject.toml").read_text()
            project = re.sub(r'(?m)^version = "[^"]+"', f'version = "{args.version}"', project, count=1)
            (destination / "pyproject.toml").write_text(project)
            for name in ("src", "migrations", "hatch_build.py", "README.md", "LICENSE"):
                path = source / name
                if path.is_dir():
                    shutil.copytree(path, destination / name, ignore=shutil.ignore_patterns("__pycache__"))
                elif path.is_file():
                    shutil.copy2(path, destination / name)
        for local in locals_:
            run("uv", "build", "--wheel", "--no-sources", "--python", VERSIONS["python"],
                "--out-dir", str(wheels), str(stage / local))
        # pip download is a build-time tool only. Refuse any third-party sdist.
        run("uv", "tool", "run", "--python", VERSIONS["python"], "--from", "pip==25.3",
            "pip", "download", "--only-binary=:all:", "--no-deps",
            "-r", str(dependencies), "--dest", str(wheels))
        triple = {"Darwin-arm64": "aarch64-apple-darwin", "Linux-x86_64": "x86_64-unknown-linux-gnu"}[target]
        archive = work / "uv.tar.gz"
        urllib.request.urlretrieve(f"https://github.com/astral-sh/uv/releases/download/{VERSIONS['uv']}/uv-{triple}.tar.gz", archive)
        with tarfile.open(archive) as tar:
            member = tar.getmember(f"uv-{triple}/uv")
            (out / "uv").write_bytes(tar.extractfile(member).read())
        (out / "uv").chmod(0o755)
        runtime = work / "python"
        run(str(out / "uv"), "python", "install", VERSIONS["python"], "--install-dir", str(runtime), "--no-bin")
        with tarfile.open(out / "python.tar.gz", "w:gz") as tar:
            tar.add(runtime, arcname="python")
    requirements = []
    packages = []
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            metadata = BytesParser().parsebytes(archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))))
        name, version = metadata["Name"], metadata["Version"]
        requirements.append(f"{name}=={version} --hash=sha256:{digest(wheel)}")
        packages.append({"name": name, "version": version, "artifact": f"wheels/{wheel.name}"})
    (out / "requirements.txt").write_text("\n".join(requirements) + "\n")
    for name in ("install.sh", "install.py"):
        content = (ROOT / "release" / name).read_text()
        (out / name).write_text(content.replace("@PYTHON_VERSION@", VERSIONS["python"]))
    manifest = {
        "schema_version": 1, "version": args.version, "source_commit": args.commit,
        "platform": target, "build_os": platform.platform(), "python": VERSIONS["python"], "uv": VERSIONS["uv"],
        "dependencies": packages,
        "runtime_components": [
            {"name": "cpython", "version": VERSIONS["python"], "artifact": "python.tar.gz"},
            {"name": "uv", "version": VERSIONS["uv"], "artifact": "uv"},
        ],
        "optional_prerequisites": ["git", "gh", "Claude/Codex CLI and credentials", "Node.js/npm for ACP agent execution"],
        "artifacts": {str(p.relative_to(out)): digest(p) for p in sorted(out.rglob("*")) if p.is_file()},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "SHA256SUMS").write_text("".join(f"{digest(p)}  {p.relative_to(out)}\n" for p in sorted(out.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"))


if __name__ == "__main__":
    main()
