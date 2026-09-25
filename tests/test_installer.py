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
        self.check_installed_launchers()

    def test_upgrade_removes_installer_owned_openengine(self):
        self.check_installed_launchers(
            '#!/bin/sh\n# Written by the OpenEngine installer; rerunning it rewrites this file.\n'
            'exec /old/release/venv/bin/engine-web "$@"\n'
        )

    def test_upgrade_preserves_unrelated_openengine(self):
        self.check_installed_launchers('#!/bin/sh\necho unrelated\n', preserve_legacy=True)

    def check_installed_launchers(self, legacy_content=None, *, preserve_legacy=False):
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
            legacy_shim = bin_dir / "openengine"
            if legacy_content is not None:
                legacy_shim.write_text(legacy_content)
                legacy_shim.chmod(0o755)
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
            # A second install must preserve the expected launchers.
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
                expected_launchers = ["engine", "openengine"] if preserve_legacy else ["engine"]
                self.assertEqual(sorted(path.name for path in bin_dir.iterdir()), expected_launchers)
                if preserve_legacy:
                    self.assertEqual(legacy_shim.read_text(), legacy_content)
