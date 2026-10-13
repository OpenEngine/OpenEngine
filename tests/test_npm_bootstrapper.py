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
        cls.archive = next(directory.glob('*.tgz'))
        with tarfile.open(cls.archive) as archive:
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
        self.env['OPENENGINE_NO_START'] = '1'

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

    def test_daemon_initialization_and_retry(self):
        self.install_fake(r"""
const fs = require('fs');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.HOME + '/calls', JSON.stringify([process.env.ENGINE_CONFIG, args]) + '\n');
if (args[1] === 'status') {
  console.log(JSON.stringify({health: process.env.HEALTH_STATE || 'ready', version: process.env.HEALTH_VERSION}));
} else if (args[1] === 'setup') process.exit(Number(process.env.SETUP_EXIT || 0));
""")
        marker = self.target / '.npm-daemon-ready'
        self.env['ENGINE_CONFIG'] = '/custom/config.toml'
        calls = self.home / 'calls'
        for args in [('daemon', 'status'), ('--help',)]:
            self.env['OPENENGINE_NO_START'] = '1'
            self.assertEqual(self.run_engine(*args).returncode, 0)
            self.assertFalse(marker.exists())
        calls.unlink()
        self.env.pop('OPENENGINE_NO_START')
        self.assertEqual(self.run_engine('daemon', 'status').returncode, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        calls.unlink()
        self.env['HEALTH_VERSION'] = '0.0.1'
        self.env['SETUP_EXIT'] = '17'
        failed = self.run_engine('--help')
        self.assertEqual(failed.returncode, 17)
        self.assertIn('engine daemon logs -n 20', failed.stderr)
        self.assertFalse(marker.exists())
        records = [json.loads(line)[1] for line in calls.read_text().splitlines()]
        self.assertEqual(records, [['daemon', 'status', '--json'], ['daemon', 'setup'], ['daemon', 'logs', '-n', '20']])
        self.env['SETUP_EXIT'] = '0'
        calls.unlink()
        self.assertEqual(self.run_engine('--version').returncode, 0)
        records = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual([r[1] for r in records], [['daemon', 'status', '--json'], ['daemon', 'setup'], ['--version']])
        self.assertTrue(all(r[0] == '/custom/config.toml' for r in records))
        calls.unlink()
        self.env['HEALTH_VERSION'] = self.version
        self.assertEqual(self.run_engine('--help').returncode, 0)
        self.assertNotIn('setup', calls.read_text())
        marker.touch()  # A marker from an older launcher must not mask a stopped service.
        calls.unlink()
        self.env['HEALTH_STATE'] = 'down'
        self.assertEqual(self.run_engine('go', 'review').returncode, 0)
        records = [json.loads(line)[1] for line in calls.read_text().splitlines()]
        self.assertEqual(records, [['daemon', 'status', '--json'], ['daemon', 'setup'], ['go', 'review']])

    def test_only_installer_shims_are_removed(self):
        self.install_fake('')
        for directory in [self.home / '.local/bin', self.home / 'custom-bin']:
            directory.mkdir(parents=True)
            if directory.name == 'custom-bin':
                self.env['XDG_BIN_HOME'] = str(directory)
            shim = directory / 'engine'
            shim.write_text('#!/bin/sh\n# Written by the OpenEngine installer; test\n')
            result = self.run_engine('--help')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(shim.exists())
            self.assertIn('replaced the curl-installer launcher', result.stderr)
            shim.write_text('user executable')
            self.assertEqual(self.run_engine('--help').returncode, 0)
            self.assertEqual(shim.read_text(), 'user executable')
            shim.unlink()
            shim.symlink_to(self.package / 'bin/engine.js')
            self.assertEqual(self.run_engine('--help').returncode, 0)
            self.assertTrue(shim.is_symlink())

    def test_shim_removal_failure_warns_and_continues(self):
        self.install_fake('')
        shim = self.home / '.local/bin/engine'
        shim.parent.mkdir(parents=True)
        shim.write_text('#!/bin/sh\n# Written by the OpenEngine installer; test\n')
        self.command = ['node', '-e', """
const fs = require('node:fs');
fs.unlinkSync = () => { throw new Error('permission denied'); };
require(process.argv[1]);
""", str(self.package / 'bin/engine.js')]
        result = self.run_engine()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('could not remove old launcher', result.stderr)
        self.assertTrue(shim.exists())

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
        self.check_file_release()

    def test_global_local_prefix(self):
        self.check_file_release(global_install=True)

    def test_global_curl_migration(self):
        self.check_file_release(global_install=True, migrate=True)

    def check_file_release(self, global_install=False, migrate=False):
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
        if global_install:
            npm_prefix = self.home / '.local'
            launcher = npm_prefix / 'bin/engine'
            if migrate:
                launcher.parent.mkdir(parents=True)
                launcher.write_text('#!/bin/sh\n# Written by the OpenEngine installer; fixture\n')
            subprocess.run(
                ['npm', 'install', '--global', '--offline', '--no-audit', '--prefix', str(npm_prefix),
                 *(['--force'] if migrate else []), str(self.archive)],
                env={**self.env, 'npm_config_cache': str(self.home / 'npm-cache')},
                check=True, capture_output=True, timeout=30,
            )
            self.assertTrue(launcher.is_symlink())
            self.command = ['node', str(launcher)]
        first = self.run_engine('go', '--help')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue((self.target / '.installed').is_file())
        if global_install:
            self.assertTrue(launcher.is_symlink())
            self.assertEqual(launcher.resolve(), (npm_prefix / 'lib/node_modules/@openengine.cc/engine/bin/engine.js').resolve())
        else:
            self.assertFalse((self.home / '.local/bin/engine').exists())
        self.assertIn('fixture:', first.stdout)
        self.assertTrue(first.stdout.rstrip().endswith(':go --help'))
        shutil.rmtree(release)
        second = self.run_engine('go')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.startswith('fixture:'))
