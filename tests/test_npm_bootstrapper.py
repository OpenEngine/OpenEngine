"""Exercise the packed npm launcher without downloading a runtime."""
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NpmBootstrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packed = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.packed.cleanup)
        directory = Path(cls.packed.name)
        subprocess.run(
            ['npm', 'pack', '--pack-destination', str(directory)],
            cwd=ROOT / 'npm/engine', check=True, capture_output=True,
            env={**os.environ, 'npm_config_cache': str(directory / 'cache')},
        )
        with tarfile.open(next(directory.glob('*.tgz'))) as archive:
            archive.extractall(directory, filter='data')
        cls.package = directory / 'package'
        cls.version = json.loads((cls.package / 'package.json').read_text())['version']

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "home with ' quotes"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('XDG_')}
        self.env.pop('ENGINE_CONFIG', None)
        self.env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.home / 'data'))
        self.prefix = self.home / 'data/openengine'
        self.target = self.prefix / 'versions' / self.version
        self.command = ['node', str(self.package / 'bin/engine.js')]

    def install_fake(self, body):
        executable = self.target / 'venv/bin/engine'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/usr/bin/env node\n' + body)
        executable.chmod(0o755)
        (self.target / '.installed').touch()

    def run_engine(self, *args):
        return subprocess.run(self.command + list(args), env=self.env, capture_output=True, text=True, timeout=20)

    def test_package_version_and_installer_parity(self):
        self.assertEqual(self.version, json.loads((ROOT / '.release-please-manifest.json').read_text())['.'])
        self.assertEqual((self.package / 'install.sh').read_bytes(), (ROOT / 'scripts/install.sh').read_bytes())
        self.assertEqual((self.package / 'LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())

    def test_skip_install_pinned_path_argv_config_and_exit(self):
        self.install_fake('console.log(JSON.stringify([process.env.ENGINE_CONFIG, ...process.argv.slice(2)])); process.exit(23);')
        self.env['OPENENGINE_RELEASE_URL'] = 'file:///does-not-exist'
        (self.prefix / 'current').symlink_to('versions/wrong-version')
        for override in ('', '/custom/config.toml'):
            self.env.update(ENGINE_CONFIG=override, XDG_CONFIG_HOME=str(self.home / 'config'))
            result = self.run_engine('go', 'argument with spaces', '', '--json')
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(json.loads(result.stdout), [override or str(self.home / 'config/openengine/engine.toml'), 'go', 'argument with spaces', '', '--json'])

    def test_default_and_empty_xdg_prefix(self):
        self.target = self.home / '.local/share/openengine/versions' / self.version
        self.install_fake('console.log(process.env.ENGINE_CONFIG);')
        for value in (None, ''):
            self.env.pop('XDG_DATA_HOME', None)
            if value is not None:
                self.env['XDG_DATA_HOME'] = value
            result = self.run_engine()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(self.home / '.config/openengine/engine.toml'))

    def test_signals_forwarded_and_reraised(self):
        self.install_fake("console.log('ready'); setInterval(() => {}, 1000);")
        for sig in (signal.SIGINT, signal.SIGTERM):
            with subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                try:
                    ready, _, _ = select.select([child.stdout], [], [], 10)
                    self.assertTrue(ready, 'engine did not start')
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                    child.send_signal(sig)
                    self.assertEqual(child.wait(timeout=10), -sig)
                finally:
                    if child.poll() is None:
                        child.kill()

    def test_child_signal_reraised(self):
        self.install_fake("process.kill(process.pid, 'SIGTERM');")
        self.assertEqual(self.run_engine().returncode, -signal.SIGTERM)

    def test_windows_guidance(self):
        result = subprocess.run(['node', '-e', "Object.defineProperty(process, 'platform', {value: 'win32'}); require(process.argv[1]);", str(self.package / 'bin/engine.js')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('wsl sh -c "curl -LsSf https://openengine.cc/install.sh | sh"', result.stderr)

    def test_installer_exit_code(self):
        package = self.home / 'package'
        shutil.copytree(self.package, package)
        (package / 'install.sh').write_text('exit 42\n')
        self.command = ['node', str(package / 'bin/engine.js')]
        self.assertEqual(self.run_engine().returncode, 42)

    def test_file_release_first_run_and_reuse(self):
        # As in test_installer.py, provide pinned uv locally. The real installer
        # still downloads, verifies and extracts the release archive.
        uv = self.prefix / 'bin/uv'
        uv.parent.mkdir(parents=True)
        uv.write_text('''#!/bin/sh
case "$1" in
  --version) echo 'uv 0.9.28' ;;
  venv)
    mkdir -p "$5/bin"
    printf '#!/bin/sh\nprintf "fixture:%%s:%%s\\n" "$ENGINE_CONFIG" "$*"\n' > "$5/bin/engine"
    chmod +x "$5/bin/engine" ;;
  pip) ;;
  *) exit 99 ;;
esac
''')
        uv.chmod(0o755)
        release = self.home / 'release'
        release.mkdir()
        bundle = self.home / f'openengine-{self.version}'
        bundle.mkdir()
        (bundle / 'requirements.txt').write_text('')
        (bundle / 'engine.toml').write_text('[state]\ndirectory = "state"\n[workflows]\ndirectory = "workflows"\n')
        archive = release / f'openengine-{self.version}.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(bundle, arcname=bundle.name)
        (release / 'release-manifest.json').write_text(json.dumps({'version': self.version, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))
        self.env['OPENENGINE_RELEASE_URL'] = release.as_uri()
        first = self.run_engine('go', '--help')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue((self.target / '.installed').is_file())
        self.assertIn('fixture:', first.stdout)
        self.assertTrue(first.stdout.rstrip().endswith(':go --help'))
        shutil.rmtree(release)
        second = self.run_engine('go')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.startswith('fixture:'))
