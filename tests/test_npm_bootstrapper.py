"""Exercise the packed npm launcher without downloading a runtime."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == 'win32'
# Fakes for the engine and uv executables run this file beside them. Windows
# runs only a real .exe, so there they are copies of a Python script launcher.
STUB = '#!python\nimport os, runpy, sys\nrunpy.run_path(os.path.splitext(sys.argv[0])[0] + ".py", run_name="__main__")\n'


def write_fake(path, body, launcher=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if launcher is not None:
        shutil.copyfile(launcher, path)
        path.with_suffix('.py').write_text(body)
    else:
        path.write_text('#!/usr/bin/env python3\n' + body)
        path.chmod(0o755)


class NpmBootstrapperTests(unittest.TestCase):
    # Whether the launcher takes its Windows path: natively on Windows, or on
    # another OS by overriding process.platform (see WindowsLayoutTests).
    windows = WINDOWS

    @classmethod
    def setUpClass(cls):
        cls.packed = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.packed.cleanup)
        directory = Path(cls.packed.name)
        subprocess.run(
            [shutil.which('npm') or 'npm', 'pack', '--pack-destination', str(directory)],
            cwd=ROOT / 'npm/engine', check=True, capture_output=True,
            env={**os.environ, 'npm_config_cache': str(directory / 'cache')},
        )
        with tarfile.open(next(directory.glob('*.tgz'))) as archive:
            archive.extractall(directory, filter='data')
        cls.package = directory / 'package'
        cls.version = json.loads((cls.package / 'package.json').read_text())['version']
        cls.launcher = None
        if WINDOWS:
            from pip._vendor.distlib.scripts import ScriptMaker

            (directory / 'stub.py').write_text(STUB)
            maker = ScriptMaker(str(directory), str(directory / 'launcher'))
            maker.executable = sys.executable
            maker.variants = {''}
            cls.launcher = directory / 'launcher' / 'stub.exe'
            maker.make('stub.py')
        cls.force_windows = directory / 'force-windows.js'
        cls.force_windows.write_text("Object.defineProperty(process, 'platform', { value: 'win32' });\n")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "home with ' quotes"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('XDG_')}
        self.env.pop('ENGINE_CONFIG', None)
        self.local = self.home / 'AppData/Local'
        self.roaming = self.home / 'AppData/Roaming'
        self.env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.home / 'data'),
                        LOCALAPPDATA=str(self.local), APPDATA=str(self.roaming))
        if self.windows and not WINDOWS:
            self.env['NODE_OPTIONS'] = f'--require "{self.force_windows}"'
        self.prefix = self.home / 'data/openengine'
        self.target = self.prefix / 'versions' / self.version
        self.command = ['node', str(self.package / 'bin/engine.js')]

    def engine_path(self, target):
        return target / ('venv/Scripts/engine.exe' if self.windows else 'venv/bin/engine')

    def install_fake(self, body):
        write_fake(self.engine_path(self.target), body, self.launcher)
        (self.target / '.installed').touch()

    def run_engine(self, *args):
        return subprocess.run(self.command + list(args), env=self.env, capture_output=True, text=True, timeout=60)

    def test_package_version_and_installer_parity(self):
        self.assertEqual(self.version, json.loads((ROOT / '.release-please-manifest.json').read_text())['.'])
        self.assertEqual((self.package / 'install.sh').read_bytes(), (ROOT / 'scripts/install.sh').read_bytes())
        self.assertEqual((self.package / 'LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())
        node = (self.package / 'bin/install.js').read_text()
        shell = (ROOT / 'scripts/install.sh').read_text()
        self.assertEqual(re.search(r"^const UV_VERSION = '([^']+)';$", node, re.M)[1],
                         re.search(r'^installer_uv_version="([^"]+)"$', shell, re.M)[1])
        self.assertEqual(re.search(r"^const PYTHON_VERSION = '([^']+)';$", node, re.M)[1],
                         re.search(r'^PYTHON_VERSION="([^"]+)"$', shell, re.M)[1])

    def test_skip_install_pinned_path_argv_config_and_exit(self):
        self.install_fake('import json, os, sys\nprint(json.dumps([os.environ.get("ENGINE_CONFIG"), *sys.argv[1:]]))\nsys.exit(23)\n')
        self.env['OPENENGINE_RELEASE_URL'] = 'file:///does-not-exist'
        if self.windows:
            (self.prefix / 'current').write_text('wrong-version\n')
        else:
            (self.prefix / 'current').symlink_to('versions/wrong-version')
        for override in ('', str(self.home / 'custom/config.toml')):
            self.env.update(ENGINE_CONFIG=override, XDG_CONFIG_HOME=str(self.home / 'config'))
            result = self.run_engine('go', 'argument with spaces', '', '--json')
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(json.loads(result.stdout), [override or str(self.home / 'config/openengine/engine.toml'), 'go', 'argument with spaces', '', '--json'])

    def test_default_and_empty_xdg_prefix(self):
        if self.windows:
            self.target = self.local / 'openengine/versions' / self.version
            config = self.roaming / 'openengine/engine.toml'
        else:
            self.target = self.home / '.local/share/openengine/versions' / self.version
            config = self.home / '.config/openengine/engine.toml'
        self.install_fake('import os\nprint(os.environ["ENGINE_CONFIG"])\n')
        for value in (None, ''):
            self.env.pop('XDG_DATA_HOME', None)
            if value is not None:
                self.env['XDG_DATA_HOME'] = value
            result = self.run_engine()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(config))

    def test_signals_forwarded_and_reraised(self):
        if self.windows:
            self.skipTest('POSIX signals')
        import select

        self.install_fake("import time\nprint('ready', flush=True)\ntime.sleep(1000)\n")
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
        self.install_fake('import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n')
        # Windows has no signal to re-raise; the launcher fails with the child.
        if self.windows:
            self.assertNotIn(self.run_engine().returncode, (0, -signal.SIGTERM))
        else:
            self.assertEqual(self.run_engine().returncode, -signal.SIGTERM)

    def test_installer_exit_code(self):
        package = self.home / 'package'
        shutil.copytree(self.package, package)
        (package / 'install.sh').write_text('exit 42\n')
        (package / 'bin/install.js').rename(package / 'bin/paths.js')
        (package / 'bin/install.js').write_text("module.exports = require('./paths.js');\nif (require.main === module) process.exit(42);\n")
        self.command = ['node', str(package / 'bin/engine.js')]
        self.assertEqual(self.run_engine().returncode, 42)

    def test_file_release_first_run_and_reuse(self):
        # As in test_installer.py, provide pinned uv locally. The real installer
        # still downloads, verifies and extracts the release archive.
        engine_body = 'import os, sys\nprint("fixture:%s:%s" % (os.environ["ENGINE_CONFIG"], " ".join(sys.argv[1:])))\n'
        write_fake(self.prefix / ('bin/uv.exe' if self.windows else 'bin/uv'), f'''import os, shutil, sys
from pathlib import Path
command = sys.argv[1]
if command == "--version":
    print("uv 0.9.28")
elif command == "venv":
    engine = Path(sys.argv[5]) / {'"Scripts/engine.exe"' if self.windows else '"bin/engine"'}
    engine.parent.mkdir(parents=True)
    if {WINDOWS}:
        shutil.copyfile(sys.argv[0], engine)
        engine.with_suffix(".py").write_text({engine_body!r})
    else:
        engine.write_text("#!/usr/bin/env python3\\n" + {engine_body!r})
        engine.chmod(0o755)
elif command != "pip":
    sys.exit(99)
''', self.launcher)
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
        # npm owns the engine launcher; the installer must not write its own.
        self.assertFalse((self.home / '.local/bin/engine').exists())
        self.assertTrue(first.stdout.rstrip().endswith(':go --help'))
        if self.windows:
            config = self.roaming / 'openengine/engine.toml'
            state, workflows = self.local / 'openengine/state', self.target / 'workflows'
            self.assertEqual((self.prefix / 'current').read_text().strip(), self.version)
        else:
            config = self.home / '.config/openengine/engine.toml'
            state, workflows = self.home / '.local/state/openengine', self.prefix / 'current/workflows'
        seeded = tomllib.loads(config.read_text())
        # install.sh names the prefix with symlinks resolved.
        self.assertEqual(Path(seeded['state']['directory']), state)
        self.assertEqual(Path(seeded['workflows']['directory']).resolve(), workflows.resolve())
        self.assertTrue(state.is_dir())
        shutil.rmtree(release)
        second = self.run_engine('go')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.startswith('fixture:'))


@unittest.skipIf(WINDOWS, 'the base tests already run natively')
class WindowsLayoutTests(NpmBootstrapperTests):
    """The launcher's and Node installer's Windows path, on any OS."""

    windows = True


if __name__ == '__main__':
    unittest.main()
