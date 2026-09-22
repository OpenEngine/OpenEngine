"""Verify and install only the packaged, hash-locked wheels."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

bundle, base = map(Path, sys.argv[1:])
manifest = json.loads((bundle / "manifest.json").read_text())
assert manifest["platform"] == f"{platform.system().lower()}-{platform.machine()}", "Wrong release platform"
assert manifest["python"] == platform.python_version(), "Wrong Python version"
for relative, expected in manifest["checksums"].items():
    assert hashlib.sha256((bundle / relative).read_bytes()).hexdigest() == expected, relative
release = base / "releases" / manifest["version"]
if release.exists():
    raise SystemExit(f"Already installed: {release}; remove it explicitly to reinstall")
release.parent.mkdir(parents=True, exist_ok=True)
uv = str(bundle / "uv")
try:
    subprocess.run([uv, "venv", "--no-config", "--managed-python", "--python", sys.executable, str(release)], check=True)
    subprocess.run([uv, "pip", "sync", "--no-config", "--python", str(release / "bin/python"),
                    "--no-index", "--no-build", "--require-hashes", "--find-links", str(bundle / "wheels"),
                    str(bundle / "requirements.txt")], check=True)
    subprocess.run([uv, "pip", "check", "--python", str(release / "bin/python")], check=True)
    shutil.copyfile(bundle / "manifest.json", release / "manifest.json")
except BaseException:
    shutil.rmtree(release, ignore_errors=True)
    raise
# A wrapper (rather than a moved virtualenv) preserves absolute interpreter paths.
bin_dir = Path.home() / ".local/bin"
bin_dir.mkdir(parents=True, exist_ok=True)
launcher = bin_dir / "engine-web"
launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(release / "bin/engine-web")) + ' "$@"\n')
launcher.chmod(0o755)
print(f"Installed OpenEngine {manifest['version']}. Start with: {launcher}")
