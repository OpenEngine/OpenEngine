"""Exercise installed launchers without downloading a runtime or release."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_engine_is_the_only_installed_launcher(self):
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
            environment = {
                **os.environ,
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_BIN_HOME": str(bin_dir),
                "OPENENGINE_RELEASE_URL": release.as_uri(),
                # CI exports this; clearing UV_* must preserve the installer pin.
                "UV_VERSION": "0.0.0",
            }
            environment.pop("ENGINE_CONFIG", None)
            # A second install must preserve the same single launcher.
            for _ in range(2):
                installed = subprocess.run(
                    ["sh", str(ROOT / "scripts/install.sh"), "--prefix", str(prefix), "--no-start"],
                    env=environment, capture_output=True, text=True,
                )
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                result = subprocess.run(
                    [str(bin_dir / "engine"), "argument with spaces", "--json"],
                    env=environment, check=True, capture_output=True, text=True,
                )
                assert result.stdout.splitlines() == [
                    str(root / "config/openengine/engine.toml"), "argument with spaces", "--json",
                ]
                self.assertEqual([path.name for path in bin_dir.iterdir()], ["engine"])
