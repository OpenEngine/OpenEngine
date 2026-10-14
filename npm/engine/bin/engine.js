#!/usr/bin/env node
'use strict';

const { spawn, spawnSync } = require('node:child_process');
const { existsSync } = require('node:fs');
const { join } = require('node:path');
const removeCurlShim = require('../scripts/cleanup-shim.js');
const { version } = require('../package.json');

// Keep a supervising process on Node 18, forwarding signals and preserving the
// child's termination status. The Python executable always lives outside npm.
function run(command, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'inherit', env });
    const forward = signal => child.kill(signal);
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
      if (signal) process.kill(process.pid, signal);
      else resolve(code);
    });
  });
}

function healthy(executable, env) {
  const result = spawnSync(executable, ['daemon', 'status', '--json'], {
    env, encoding: 'utf8', timeout: 10000,
  });
  try {
    const status = JSON.parse(result.stdout);
    return result.status === 0 && status.health === 'ready' && status.version === version;
  } catch { return false; }
}

async function main() {
  if (process.platform === 'win32') {
    throw new Error('Use @openengine.cc/engine inside WSL: wsl sh -c "curl -LsSf https://openengine.cc/install.sh | sh"');
  }
  const env = { ...process.env };
  if (!env.HOME) throw new Error('HOME is not set');
  env.ENGINE_CONFIG = env.ENGINE_CONFIG || join(env.XDG_CONFIG_HOME || join(env.HOME, '.config'), 'openengine/engine.toml');
  removeCurlShim(join(env.XDG_BIN_HOME || join(env.HOME, '.local/bin'), 'engine'));
  const prefix = join(env.XDG_DATA_HOME || join(env.HOME, '.local/share'), 'openengine');
  const target = join(prefix, 'versions', version);
  if (!existsSync(join(target, '.installed'))) {
    const code = await run('sh', [join(__dirname, '../install.sh'), '--version', version, '--no-start', '--no-shim'], env);
    if (code !== 0) return code;
  }
  const executable = join(target, 'venv/bin/engine');
  const args = process.argv.slice(2);
  if (args[0] !== 'daemon' && env.OPENENGINE_NO_START !== '1') {
    if (!healthy(executable, env)) {
      const code = await run(executable, ['daemon', 'start', '--upgrade'], env);
      if (code !== 0) {
        console.error('engine: daemon start failed; inspect logs with `engine daemon logs -n 20`');
        await run(executable, ['daemon', 'logs', '-n', '20'], env);
        return code;
      }
    }
  }
  return run(executable, args, env);
}

main().then(code => { process.exitCode = code; }).catch(error => {
  console.error(`@openengine.cc/engine: ${error.message}`);
  process.exitCode = 1;
});
