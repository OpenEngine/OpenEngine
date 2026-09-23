"""Second-stage installer, run only by the private bundled interpreter."""
import hashlib
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

bundle, prefix = map(Path, sys.argv[1:])
manifest = json.loads((bundle / "manifest.json").read_text())
if manifest["platform"] != f"{platform.system()}-{platform.machine()}":
    raise SystemExit("Release platform does not match this machine")
if manifest["python"] != platform.python_version():
    raise SystemExit("Release interpreter does not match the manifest")
for name, expected in manifest["artifacts"].items():
    with (bundle / name).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise SystemExit(f"Checksum mismatch: {name}")
subprocess.run([
    str(prefix / "uv"), "pip", "install", "--python", sys.executable,
    "--no-index", "--no-build", "--require-hashes", "--find-links", str(bundle / "wheels"),
    "-r", str(bundle / "requirements.txt"),
], check=True)
subprocess.run([str(prefix / "uv"), "pip", "check", "--python", sys.executable], check=True)
shutil.copy2(bundle / "manifest.json", prefix / "manifest.json")
bin_dir = prefix / "bin"
bin_dir.mkdir()
launcher = bin_dir / "engine-web"
launcher.write_text("#!/bin/sh\nexec " + shlex.quote(str(prefix / "venv/bin/engine-web")) + ' "$@"\n')
launcher.chmod(0o755)
print(f"Installed OpenEngine {manifest['version']}. Start with: {launcher}")
