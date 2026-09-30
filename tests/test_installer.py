"""Exercise installed launchers without downloading a runtime or release."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_engine_is_the_only_installed_launcher(self):
        self._install("v20.19.0")

    def test_bundles_node_when_missing_or_old_and_reuses_it(self):
        for version in (None, "v20.18.9"):
            with self.subTest(version=version):
                self._install(version)

    def test_all_node_targets(self):
        for target in ("darwin-arm64", "darwin-x64", "linux-arm64"):
            with self.subTest(target=target):
                self._install(None, node_target=target)

    def test_musl_warns_and_completes_without_node_download(self):
        self._install(None, musl=True)

    def test_rejects_node_checksum_mismatch(self):
        self._install(None, corrupt=True)

    def _install(self, node_version, musl=False, corrupt=False, node_target="linux-x64"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "release with ' quotes"
            target = prefix / "versions" / "1.2.3"
            executables = target / "venv" / "bin"
            executables.mkdir(parents=True)
            (target / ".installed").touch()
            (target / "engine.toml").write_text(
                '[state]\ndirectory = "state"\n[workflows]\ndirectory = "workflows"\n'
            )
            engine = executables / "engine"
            engine.write_text('#!/bin/sh\nprintf "%s\\n" "$ENGINE_CONFIG" "$@"\n')
            engine.chmod(0o755)
            (prefix / "bin").mkdir()
            uv = prefix / "bin" / "uv"
            uv.write_text('#!/bin/sh\necho "uv 0.9.28"\n')
            uv.chmod(0o755)
            release = root / "release"
            release.mkdir()
            (release / "release-manifest.json").write_text(json.dumps(
                {"version": "1.2.3", "archive_sha256": "unused-already-installed"},
                indent=2,
            ))
            bin_dir = root / "bin"
            bin_dir.mkdir()
            # An isolated PATH makes all scenarios independent of the CI host.
            tools = root / "tools"
            tools.mkdir()
            for name in ("sh", "cat", "tar", "gzip", "mkdir", "mktemp", "shasum", "cut", "sed",
                         "env", "awk", "rm", "mv", "ln", "chmod", "grep", "basename"):
                (tools / name).symlink_to(shutil.which(name))

            def executable(name, body):
                path = tools / name
                path.write_text("#!/bin/sh\n" + body)
                path.chmod(0o755)

            system = "Darwin" if node_target.startswith("darwin") else "Linux"
            cpu = "arm64" if node_target.endswith("arm64") else "x86_64"
            executable("uname", f'case "$1" in -s) echo {system} ;; -m) echo {cpu} ;; esac\n')
            executable("ldd", "echo " + ("musl" if musl else "glibc") + "\n")
            if node_version:
                executable("node", f"echo {node_version}\n")
            script = (ROOT / "scripts/install.sh").read_text()
            pin = re.search(r'installer_node_version="([^"]+)"', script).group(1)
            node_tree = root / f"node-v{pin}-{node_target}"
            (node_tree / "bin").mkdir(parents=True)
            for name, body in (("node", f"echo v{pin}"), ("npx", 'exec node --version')):
                path = node_tree / "bin" / name
                path.write_text("#!/bin/sh\n" + body + "\n")
                path.chmod(0o755)
            archive = root / "node.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(node_tree, arcname=node_tree.name)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            script = re.sub(r"node_sha256=[a-f0-9]{64}", "node_sha256=" + ("0" * 64 if corrupt else digest), script)
            installer = root / "install.sh"
            installer.write_text(script)
            # Mock only transport: the installer still extracts and hashes the archive.
            executable("curl", f'''output=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in --output) shift; output=$1 ;; https://*|file://*) url=$1 ;; esac
  shift
done
case "$url" in
  https://nodejs.org/*)
    echo download >>"$HOME/downloads"
    {shutil.which("cp")} "$HOME/node.tar.gz" "$output" ;;
  *) exec {shutil.which("curl")} --proto '=https,file' --fail --silent --show-error --output "$output" "$url" ;;
esac
''')
            environment = {
                **os.environ,
                "HOME": str(root),
                "PATH": str(tools),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_BIN_HOME": str(bin_dir),
                "OPENENGINE_RELEASE_URL": release.as_uri(),
                # CI exports this; clearing UV_* must preserve the installer pin.
                "UV_VERSION": "0.0.0",
            }
            environment.pop("ENGINE_CONFIG", None)
            environment.pop("ENGINE_NODE_BIN", None)
            # A second install must preserve the same single launcher.
            for _ in range(2):
                installed = subprocess.run(
                    ["sh", str(installer), "--prefix", str(prefix), "--no-start"],
                    env=environment, capture_output=True, text=True,
                )
                if corrupt:
                    self.assertNotEqual(installed.returncode, 0)
                    self.assertIn("SHA-256 mismatch", installed.stderr)
                    self.assertFalse((prefix / "node" / pin).exists())
                    return
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                if musl:
                    self.assertIn("apk add nodejs npm", installed.stderr)
                downloads = root / "downloads"
                needs_bundle = node_version != "v20.19.0" and not musl
                self.assertEqual(downloads.read_text().splitlines() if downloads.exists() else [],
                                 ["download"] if needs_bundle else [])
                if needs_bundle:
                    # Launch through the actual installed shim to verify npx finds Node.
                    engine.write_text('#!/bin/sh\nnpx --version\n')
                    probe = subprocess.run([str(bin_dir / "engine")], env=environment,
                                           capture_output=True, text=True, check=True)
                    self.assertEqual(probe.stdout.strip(), f"v{pin}")
                    engine.write_text('#!/bin/sh\nprintf "%s\\n" "$ENGINE_CONFIG" "$@"\n')
                result = subprocess.run(
                    [str(bin_dir / "engine"), "argument with spaces", "--json"],
                    env=environment, check=True, capture_output=True, text=True,
                )
                assert result.stdout.splitlines() == [
                    str(root / "config/openengine/engine.toml"), "argument with spaces", "--json",
                ]
                self.assertEqual([path.name for path in bin_dir.iterdir()], ["engine"])
