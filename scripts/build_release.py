"""Build every distribution and a checksummed application bundle (Python 3.11+)."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from email.parser import BytesParser
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build(commit: str) -> Path:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = project["project"]["version"]
    workspace = project["tool"]["uv"]["workspace"]
    sources = {ROOT, ROOT / "langgraph-acp"}
    excluded = {p for pattern in workspace["exclude"] for p in ROOT.glob(pattern)}
    for pattern in workspace["members"]:
        sources.update(p for p in ROOT.glob(pattern) if p not in excluded)
    expected = {
        tomllib.loads((p / "pyproject.toml").read_text())["project"]["name"]
        for p in sources
    }
    subprocess.run(["npm", "--prefix", "apps/web", "run", "build"], cwd=ROOT, check=True)
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary) / f"openengine-{version}"
        wheels = bundle / "wheels"
        wheels.mkdir(parents=True)
        for arguments in (["--all-packages"], ["langgraph-acp"]):
            subprocess.run(
                ["uv", "build", *arguments, "--wheel", "--out-dir", str(wheels)],
                cwd=ROOT,
                check=True,
            )
        distributions = {}
        for wheel in sorted(wheels.glob("*.whl")):
            with ZipFile(wheel) as archive:
                metadata_path = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                metadata = BytesParser().parsebytes(archive.read(metadata_path))
                name = metadata["Name"]
                if name in distributions:
                    raise ValueError(f"Duplicate wheel: {name}")
                if metadata["Version"] != version:
                    raise ValueError(f"Unexpected version for {name}: {metadata['Version']}")
                if name == "engine-web" and "engine/apps/web/static/index.html" not in archive.namelist():
                    raise ValueError("Web wheel is missing the built client")
                distributions[name] = {"version": metadata["Version"], "wheel": f"wheels/{wheel.name}"}
        if distributions.keys() != expected:
            raise ValueError(f"Wheel inventory mismatch: {distributions.keys() ^ expected}")
        shutil.copytree(ROOT / "workflows", bundle / "workflows", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, bundle / name)
        files = [
            {"path": str(p.relative_to(bundle)), "size": p.stat().st_size,
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(bundle.rglob("*")) if p.is_file()
        ]
        manifest = {"schema_version": 1, "version": version, "commit": commit,
                    "distributions": distributions, "files": files}
        (bundle / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        archive_path = output / f"openengine-{version}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(bundle, arcname=bundle.name)
        shutil.copy2(bundle / "release-manifest.json", output / "release-manifest.json")
    return archive_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Source commit recorded in the manifest")
    print(build(parser.parse_args().commit))
