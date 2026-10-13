#!/usr/bin/env node
'use strict';

const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const { join } = require('node:path');
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

async function main() {
  if (process.platform === 'win32') {
    throw new Error('Use @openengine.cc/engine inside WSL: wsl sh -c "curl -LsSf https://openengine.cc/install.sh | sh"');
  }
  const env = process.env;
  if (!env.HOME) throw new Error('HOME is not set');
  const prefix = join(env.XDG_DATA_HOME || join(env.HOME, '.local/share'), 'openengine');
  const target = join(prefix, 'versions', version);
  if (!existsSync(join(target, '.installed'))) {
    const code = await run('sh', [join(__dirname, '../install.sh'), '--version', version, '--no-start'], env);
    if (code !== 0) return code;
  }
  return run(join(target, 'venv/bin/engine'), process.argv.slice(2), {
    ...env,
    ENGINE_CONFIG: env.ENGINE_CONFIG || join(env.XDG_CONFIG_HOME || join(env.HOME, '.config'), 'openengine/engine.toml'),
  });
}

main().then(code => { process.exitCode = code; }).catch(error => {
  console.error(`@openengine.cc/engine: ${error.message}`);
  process.exitCode = 1;
});
