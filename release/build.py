"""Build a native release bundle from uv.lock and an already-built frontend.

Run with the pinned release interpreter; CI supplies version and source commit.
Only CI builds source. The installer consumes the resulting wheelhouse offline.
"""
import argparse
import email
import hashlib
import json
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

PYTHON = "3.14.7"
UV = "0.12.7"


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev\d+)?", args.version):
        parser.error("version must be X.Y.Z or X.Y.Z.devN")
    if platform.python_version() != PYTHON:
        parser.error(f"build with Python {PYTHON}")
    root = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    wheels = out / "wheels"
    wheels.mkdir()
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        exported = temp / "export.txt"
        run("uv", "export", "--locked", "--package", "engine-web", "--no-dev",
            "--no-editable", "--no-header", "--output-file", str(exported), cwd=root,
            stdout=subprocess.DEVNULL)
        lines = exported.read_text().splitlines(keepends=True)
        locals_ = [line.strip() for line in lines if line.startswith(".")]
        requirements = temp / "dependencies.txt"
        requirements.write_text("".join(line for line in lines if not line.startswith(".")))
        # pip is a build tool only. All third-party artifacts must be wheels.
        run(sys.executable, "-m", "pip", "download", "--only-binary=:all:",
            "--require-hashes", "--no-deps", "-r", str(requirements), "-d", str(wheels))
        stage = temp / "source"
        shutil.copytree(root, stage, ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"))
        for relative in locals_:
            project = stage / relative
            metadata = project / "pyproject.toml"
            metadata.write_text(re.sub(r'^version = ".*?"', f'version = "{args.version}"',
                                       metadata.read_text(), count=1, flags=re.M))
            run("uv", "build", "--wheel", "--no-sources", "--out-dir", str(wheels), str(project))
    dependencies = {}
    pins = []
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            metadata = email.message_from_bytes(archive.read(name))
        dependencies[metadata["Name"]] = metadata["Version"]
        pins.append(f'{metadata["Name"]}=={metadata["Version"]} --hash=sha256:{digest(wheel)}\n')
    (out / "requirements.txt").write_text("".join(pins))
    for name in ("install.sh", "install.py", "smoke.py"):
        shutil.copyfile(root / "release" / name, out / name)
    uv = Path(shutil.which("uv")).resolve()
    assert subprocess.check_output([str(uv), "--version"], text=True).split()[1] == UV
    shutil.copyfile(uv, out / "uv")
    (out / "uv").chmod(0o755)
    manifest = {
        "schema_version": 1, "version": args.version, "source_commit": args.commit,
        "python": PYTHON, "uv": UV,
        "platform": f"{platform.system().lower()}-{platform.machine()}",
        "build_platform": platform.platform(),
        "dependencies": dependencies,
        "runtime_components": [{"name": "cpython", "version": PYTHON,
                                "provision": f"./uv python install --managed-python {PYTHON}",
                                "install_directory": "<user-data>/OpenEngine/python"},
                               {"name": "uv", "version": UV, "provision": "bundled ./uv"}],
        "checksums": {str(p.relative_to(out)): digest(p) for p in sorted(out.rglob("*")) if p.is_file()},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "SHA256SUMS").write_text("".join(
        f"{digest(p)}  {p.relative_to(out)}\n" for p in sorted(out.rglob("*")) if p.is_file()))


if __name__ == "__main__":
    main()
