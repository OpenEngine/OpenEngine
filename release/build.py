"""Assemble a platform wheelhouse from uv.lock; never resolve at install time."""
import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "3.12.12"
UV = "0.9.28"


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.+]*)?", args.version):
        parser.error("version must be a PEP 440 release version")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("commit must be the full source SHA")
    system, machine = platform.system(), platform.machine()
    targets = {("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
               ("Darwin", "arm64"): "aarch64-apple-darwin"}
    target = targets[(system, machine)]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    wheels = out / "wheels"
    wheels.mkdir()
    exported = run("uv", "export", "--locked", "--package", "engine-web",
                   "--no-dev", "--no-editable", "--no-annotate", "--no-header")
    local = [line for line in exported.splitlines() if line.startswith(".")]
    third_party = "\n".join(line for line in exported.splitlines() if not line.startswith("."))
    (out / "dependencies.lock").write_text(third_party + "\n")
    # pip downloads wheels for the interpreter running it, including marker evaluation.
    run("uv", "tool", "run", "--python", PYTHON, "--from", "pip==25.3", "pip",
        "download", "--only-binary=:all:", "--no-deps", "--require-hashes",
        "-r", str(out / "dependencies.lock"), "--dest", str(wheels))
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source"
        shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "__pycache__", "dist", "build"))
        shutil.copytree(ROOT / "apps/web/dist", source / "apps/web/dist")
        for relative in local:
            project = source / relative
            metadata = project / "pyproject.toml"
            metadata.write_text(re.sub(r'^version = "[^"]+"',
                                      f'version = "{args.version}"', metadata.read_text(),
                                      count=1, flags=re.MULTILINE))
            run("uv", "build", "--wheel", "--no-sources", "--out-dir", str(wheels), str(project))
    uv_url = f"https://github.com/astral-sh/uv/releases/download/{UV}/uv-{target}.tar.gz"
    archive = out / "uv.tar.gz"
    urllib.request.urlretrieve(uv_url, archive)
    with tarfile.open(archive) as tar:
        member = tar.getmember(f"uv-{target}/uv")
        with tar.extractfile(member) as stream:
            (out / "uv").write_bytes(stream.read())
    (out / "uv").chmod(0o755)
    archive.unlink()
    for name in ("install.sh", "smoke.py"):
        shutil.copy2(ROOT / "release" / name, out / name)
    shutil.copy2(ROOT / "LICENSE", out / "LICENSE")
    shutil.copy2(ROOT / "NOTICE", out / "NOTICE")
    dependencies = []
    for wheel in sorted(wheels.glob("*.whl")):
        name, version, *_ = wheel.name.split("-")
        dependencies.append(f"{name}=={version} --hash=sha256:{digest(wheel)}")
    (out / "requirements.txt").write_text("\n".join(dependencies) + "\n")
    manifest = dict(schema_version=1, version=args.version, source_commit=args.commit,
                    platform=target, tested_architecture=machine, python=PYTHON,
                    runtime={"provider": "uv managed CPython", "version": PYTHON,
                             "provision": f"uv python install --no-bin {PYTHON}",
                             "uv_version": UV, "uv_source": uv_url},
                    startup_components=[], dependencies=dependencies,
                    artifacts={str(p.relative_to(out)): digest(p)
                               for p in sorted(out.rglob("*")) if p.is_file()})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "SHA256SUMS").write_text("".join(
        f"{digest(p)}  {p.relative_to(out)}\n" for p in sorted(out.rglob("*"))
        if p.is_file() and p.name != "SHA256SUMS"))


if __name__ == "__main__":
    main()
