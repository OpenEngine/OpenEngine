#!/usr/bin/env node
'use strict';

// Install an OpenEngine release for the current Windows user, as
// scripts/install.sh does on macOS and Linux:
//
//   node install.js [--version X.Y.Z]
//
// Python comes from a pinned uv that is kept apart from any uv, Python, or
// configuration already on the machine. Running it again is safe: an installed
// version is reused, and an existing engine.toml is never touched. npm owns the
// `engine` launcher, so this writes none and starts nothing.
//
// OPENENGINE_RELEASE_URL names a directory holding release-manifest.json and
// the archive (https:// or file://), in place of the GitHub release.

const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const { isAbsolute, join, sep } = require('node:path');
const { fileURLToPath } = require('node:url');

const REPOSITORY = 'OpenEngine/OpenEngine';
const PYTHON_VERSION = '3.12';
const UV_VERSION = '0.9.28';
const UV_SHA256 = {
  'x86_64-pc-windows-msvc': '9cb567fcd92f31431220ce620787043b946c30b9bb46ca213780e5ef471453be',
  'aarch64-pc-windows-msvc': '081703fa19ae05a49f486f97468f7792e1cdacda403a091b151af7f5bd6f4595',
};
const UV_CPU = { x64: 'x86_64', arm64: 'aarch64' };

class InstallError extends Error {}
const say = message => console.log(`openengine: ${message}`);
const die = message => { throw new InstallError(message); };

// %LOCALAPPDATA% and %APPDATA% unless XDG_* names an absolute directory, as
// on the other platforms. `engine daemon` resolves the same state directory,
// so this follows pathlib's rule: on Windows a drive or UNC root, never a bare
// `\data`. Windows ignores the owner-only modes used here and by the daemon,
// so an XDG_* override should stay inside the user profile (see docs/releases.md).
function windowsPaths(env = process.env) {
  const absolute = value => isAbsolute(value) && (sep === '/' || /^([A-Za-z]:|[\\/]{2})/.test(value));
  const xdg = name => env[name] && absolute(env[name]) ? env[name] : '';
  const local = env.LOCALAPPDATA || die('LOCALAPPDATA is not set');
  const roaming = env.APPDATA || die('APPDATA is not set');
  return {
    prefix: join(xdg('XDG_DATA_HOME') || local, 'openengine'),
    config: join(xdg('XDG_CONFIG_HOME') || roaming, 'openengine', 'engine.toml'),
    state: xdg('XDG_STATE_HOME') ? join(env.XDG_STATE_HOME, 'openengine') : join(local, 'openengine', 'state'),
    cache: xdg('XDG_CACHE_HOME') ? join(env.XDG_CACHE_HOME, 'openengine') : join(local, 'openengine', 'cache'),
  };
}

const executable = (target, name) => join(target, 'venv', 'Scripts', `${name}.exe`);

// HTTPS only, redirects included, so no plain-HTTP hop can swap the manifest
// and the archive it vouches for. file:// is for OPENENGINE_RELEASE_URL in CI.
async function fetchTo(url, destination) {
  if (url.startsWith('file:')) {
    try {
      fs.copyFileSync(fileURLToPath(url), destination);
      return;
    } catch (error) {
      die(`could not download ${url}: ${error.message}`);
    }
  }
  let problem;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      let location = url;
      for (let hop = 0; hop < 10; hop++) {
        if (new URL(location).protocol !== 'https:') die(`refusing to download ${location}: not https`);
        const response = await fetch(location, { redirect: 'manual' });
        if (response.status >= 300 && response.status < 400 && response.headers.get('location')) {
          location = new URL(response.headers.get('location'), location).href;
          continue;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        fs.writeFileSync(destination, Buffer.from(await response.arrayBuffer()));
        return;
      }
      throw new Error('too many redirects');
    } catch (error) {
      if (error instanceof InstallError) throw error;
      problem = error;
    }
  }
  die(`could not download ${url}: ${problem.message}`);
}

function verify(file, expected) {
  const actual = createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  if (actual !== expected) die(`SHA-256 mismatch for ${file}: expected ${expected}, got ${actual}`);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', ...options });
  if (result.error) die(`could not run ${command}: ${result.error.message}`);
  if (result.status !== 0) die(`${command} ${args[0]} exited with ${result.status}`);
}

// Windows' own bsdtar unpacks both .tar.gz and .zip; a Git or MSYS tar earlier
// on PATH would read `C:` in a path as a remote host.
function untar(archive, directory) {
  const root = process.env.SystemRoot;
  const tar = root && fs.existsSync(join(root, 'System32', 'tar.exe')) ? join(root, 'System32', 'tar.exe') : 'tar';
  run(tar, ['-xf', archive, '-C', directory]);
}

function tomlString(value) {
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

// The [state] and [workflows] directories of the release's default config,
// pointed at this install; any other layout is refused rather than guessed.
function seedConfig(template, state, workflows) {
  let section = '';
  let s = 0;
  let w = 0;
  const lines = template.split(/\r?\n/);
  if (lines[lines.length - 1] === '') lines.pop();
  const output = lines.map(line => {
    if (line.startsWith('[')) section = line;
    if (/^directory[ \t]*=/.test(line)) {
      if (section === '[state]') { s++; return `directory = ${tomlString(state)}`; }
      if (section === '[workflows]') { w++; return `directory = ${tomlString(workflows)}`; }
    }
    return line;
  });
  if (s !== 1 || w !== 1) die("the release's default engine.toml has an unexpected layout");
  return ['# Written by the OpenEngine installer, which never overwrites it.', '', ...output, ''].join('\n');
}

// Nothing from the caller's uv setup applies: no configuration files, no UV_*
// variables, no active virtualenv, and only the Python uv downloads itself.
function uvEnvironment(prefix, cache) {
  const env = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (/^UV_/i.test(name) || /^(VIRTUAL_ENV|CONDA_PREFIX|PYTHONHOME|PYTHONPATH)$/i.test(name)) continue;
    env[name] = value;
  }
  return {
    ...env,
    UV_NO_CONFIG: '1',
    UV_PYTHON_INSTALL_DIR: join(prefix, 'python'),
    UV_PYTHON_PREFERENCE: 'only-managed',
    UV_CACHE_DIR: join(cache, 'uv'),
  };
}

async function install(argv) {
  let version = '';
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--version' && i + 1 < argv.length) version = argv[++i];
    else if (argv[i].startsWith('--version=')) version = argv[i].slice('--version='.length);
    else die(`unknown option: ${argv[i]}`);
  }
  version = version.replace(/^v/, '');
  const { prefix, config, state, cache } = windowsPaths();
  const cpu = UV_CPU[process.arch] || die(`unsupported CPU ${process.arch}; OpenEngine installs on x64 and arm64`);
  const uvTarget = `${cpu}-pc-windows-msvc`;

  fs.mkdirSync(join(prefix, 'bin'), { recursive: true });
  fs.mkdirSync(join(prefix, 'versions'), { recursive: true });
  const work = fs.mkdtempSync(join(prefix, '.install.'));
  try {
    const env = uvEnvironment(prefix, cache);
    const uv = join(prefix, 'bin', 'uv.exe');
    const current = spawnSync(uv, ['--version'], { env, encoding: 'utf8' });
    if (!new RegExp(`^uv ${UV_VERSION.replace(/\./g, '\\.')}( |$)`).test((current.stdout || '').trim())) {
      say(`downloading uv ${UV_VERSION}`);
      const archive = join(work, 'uv.zip');
      await fetchTo(`https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uvTarget}.zip`, archive);
      verify(archive, UV_SHA256[uvTarget]);
      untar(archive, work);
      fs.renameSync(join(work, 'uv.exe'), uv);
    }

    let releaseUrl;
    if (process.env.OPENENGINE_RELEASE_URL) {
      releaseUrl = process.env.OPENENGINE_RELEASE_URL.replace(/\/+$/, '');
      if (!/^(https|file):\/\//.test(releaseUrl)) die('OPENENGINE_RELEASE_URL must start with https:// or file://');
    } else if (version) {
      releaseUrl = `https://github.com/${REPOSITORY}/releases/download/v${version}`;
    } else {
      releaseUrl = `https://github.com/${REPOSITORY}/releases/latest/download`;
    }
    const manifestPath = join(work, 'release-manifest.json');
    await fetchTo(`${releaseUrl}/release-manifest.json`, manifestPath);
    let manifest;
    try {
      manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    } catch (error) {
      die(`release-manifest.json at ${releaseUrl} is not JSON: ${error.message}`);
    }
    const release = manifest && manifest.version;
    if (typeof release !== 'string' || !/^[0-9A-Za-z+-][0-9A-Za-z.+-]*$/.test(release)) {
      die(`release-manifest.json at ${releaseUrl} names no valid version`);
    }
    if (version && version !== release) die(`requested version ${version}, but the release manifest is for ${release}`);
    const archiveSha256 = manifest.archive_sha256;
    if (typeof archiveSha256 !== 'string' || !archiveSha256) die(`release ${release} predates this installer; choose a newer --version`);

    const target = join(prefix, 'versions', release);
    if (fs.existsSync(join(target, '.installed'))) {
      say(`OpenEngine ${release} is already installed`);
    } else {
      say(`downloading OpenEngine ${release}`);
      const archive = join(work, `openengine-${release}.tar.gz`);
      await fetchTo(`${releaseUrl}/openengine-${release}.tar.gz`, archive);
      verify(archive, archiveSha256);
      untar(archive, work);
      const bundle = join(work, `openengine-${release}`);
      if (!fs.existsSync(join(bundle, 'requirements.txt'))) die(`the archive holds no openengine-${release} bundle`);
      // A directory without the marker is an install that did not finish.
      fs.rmSync(target, { recursive: true, force: true });
      fs.renameSync(bundle, target);
      say(`installing OpenEngine ${release} with Python ${PYTHON_VERSION}`);
      run(uv, ['venv', '--quiet', '--python', PYTHON_VERSION, join(target, 'venv')], { env });
      run(uv, ['pip', 'install', '--quiet', '--python', executable(target, 'python'),
        '--require-hashes', '-r', 'requirements.txt'], { env, cwd: target });
      fs.writeFileSync(join(target, '.installed'), '');
    }
    // No symlink: the active version, by name.
    fs.writeFileSync(join(prefix, 'current'), `${release}\n`);

    fs.mkdirSync(state, { recursive: true });
    if (fs.existsSync(config)) {
      say(`keeping the existing ${config}`);
    } else {
      fs.mkdirSync(join(config, '..'), { recursive: true });
      const seeded = seedConfig(fs.readFileSync(join(target, 'engine.toml'), 'utf8'), state, join(target, 'workflows'));
      try {
        // 'wx': a config that appeared since the check above is still not replaced.
        fs.writeFileSync(config, seeded, { flag: 'wx', mode: 0o600 });
        say(`wrote ${config}`);
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
      }
    }
    say(`installed OpenEngine ${release}: ${target}`);
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
}

module.exports = { executable, install, windowsPaths };

if (require.main === module) {
  install(process.argv.slice(2)).catch(error => {
    console.error(`openengine: error: ${error.message}`);
    process.exitCode = 1;
  });
}
