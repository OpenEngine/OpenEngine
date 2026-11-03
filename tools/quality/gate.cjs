const {spawnSync} = require('node:child_process');
const path = require('node:path');
// Keep stdout exclusively machine-readable; test progress belongs on stderr.
for (const [args, stdio] of [
  [['--test', path.join(__dirname, 'duplication.test.cjs')], ['inherit', 2, 2]],
  [['--max-old-space-size=4096', path.join(__dirname, 'build/duplication.js'), ...process.argv.slice(2)], 'inherit'],
]) {
  const result = spawnSync(process.execPath, args, {stdio});
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}
