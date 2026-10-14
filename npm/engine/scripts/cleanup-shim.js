'use strict';
const { openSync, readSync, closeSync, statSync, unlinkSync } = require('node:fs');
const { join } = require('node:path');
// Only remove an installer-owned launcher, never the running npm executable.
function removeCurlShim(path) {
  let fd;
  try {
    const candidate = statSync(path);
    const running = statSync(join(__dirname, '../bin/engine.js'));
    if (!candidate.isFile() || (candidate.dev === running.dev && candidate.ino === running.ino)) return;
    fd = openSync(path, 'r');
    const buffer = Buffer.alloc(1024);
    const length = readSync(fd, buffer, 0, buffer.length, 0);
    if (!buffer.toString('utf8', 0, length).split('\n').slice(0, 5)
      .some(line => line.startsWith('# Written by the OpenEngine installer'))) return;
    unlinkSync(path);
    console.error('engine: replaced the curl-installer launcher');
  } catch (error) {
    if (error.code !== 'ENOENT') console.error(`engine: could not remove old launcher ${path}: ${error.message}`);
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

module.exports = removeCurlShim;

if (require.main === module && process.env.npm_config_global === 'true') {
  const env = process.env;
  if (env.HOME) removeCurlShim(join(env.XDG_BIN_HOME || join(env.HOME, '.local/bin'), 'engine'));
}
