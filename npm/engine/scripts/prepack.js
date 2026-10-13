const { copyFileSync } = require('node:fs');
const { resolve } = require('node:path');

for (const [source, destination] of [['scripts/install.sh', 'install.sh'], ['LICENSE', 'LICENSE']]) {
  copyFileSync(resolve(__dirname, '../../..', source), resolve(__dirname, '..', destination));
}
