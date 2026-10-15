#!/usr/bin/env node
'use strict';

const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const { join } = require('node:path');
const { version } = require('../package.json');

const windows = process.platform === 'win32';

// Keep a supervising process on Node 18, forwarding signals and preserving the
// child's termination status. The Python executable always lives outside npm.
function run(command, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'inherit', env });
    // On Windows the console delivers Ctrl+C to the child itself, and kill()
    // would terminate it outright; the launcher only waits for it.
    const forward = signal => { if (!windows) child.kill(signal); };
    const interrupt = () => forward('SIGINT');
    const terminate = () => forward('SIGTERM');
    process.on('SIGINT', interrupt);
    process.on('SIGTERM', terminate);
    const cleanup = () => {
      process.removeListener('SIGINT', interrupt);
      process.removeListener('SIGTERM', terminate);
    };
    child.once('error', error => { cleanup(); reject(error); });
    child.once('exit', (code, signal) => {
      cleanup();
      // Windows has no signal to re-raise: the child's exit code is its status.
      if (signal && !windows) process.kill(process.pid, signal);
      else resolve(code === null ? 1 : code);
    });
  });
}

// Where the release, its engine executable and the default config live.
function paths(env) {
  if (windows) {
    const { executable, windowsPaths } = require('./install.js');
    const { prefix, config } = windowsPaths(env);
    const target = join(prefix, 'versions', version);
    return { target, config, engine: executable(target, 'engine'),
      install: [process.execPath, [join(__dirname, 'install.js'), '--version', version]] };
  }
  if (!env.HOME) throw new Error('HOME is not set');
  const target = join(env.XDG_DATA_HOME || join(env.HOME, '.local/share'), 'openengine', 'versions', version);
  return { target, config: join(env.XDG_CONFIG_HOME || join(env.HOME, '.config'), 'openengine/engine.toml'),
    engine: join(target, 'venv/bin/engine'),
    install: ['sh', [join(__dirname, '../install.sh'), '--version', version, '--no-start', '--no-shim']] };
}

async function main() {
  const env = process.env;
  const { target, config, engine, install } = paths(env);
  if (!existsSync(join(target, '.installed'))) {
    const code = await run(...install, env);
    if (code !== 0) return code;
  }
  return run(engine, process.argv.slice(2), { ...env, ENGINE_CONFIG: env.ENGINE_CONFIG || config });
}

main().then(code => { process.exitCode = code; }).catch(error => {
  console.error(`@openengine.cc/engine: ${error.message}`);
  process.exitCode = 1;
});
