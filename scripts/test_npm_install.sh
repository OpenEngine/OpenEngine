#!/usr/bin/env bash
# Release workflow smoke tests: packed npm package, local real Python bundle.
set -euo pipefail

if [ "$SCENARIO" != npm-default ]; then
  npm config set prefix "$HOME/.local"
fi
npm_prefix=$(npm prefix --global)
export PATH="$npm_prefix/bin:$PATH"
[ ! -e "$HOME/.local/bin/engine" ]

if [ "$SCENARIO" = npm-migrate ]; then
  sh scripts/install.sh --no-start
  grep -q '^# Written by the OpenEngine installer' "$HOME/.local/bin/engine"
fi
(cd npm/engine && npm pack --pack-destination "$RUNNER_TEMP")
archives=("$RUNNER_TEMP"/openengine.cc-engine-*.tgz)
archive=${archives[0]}
if [ "$SCENARIO" = npm-migrate ]; then
  npm i -g --force "$archive"
else
  npm i -g "$archive"
fi
trap 'engine daemon stop || true' EXIT
assert_npm_link() {
  test -L "$npm_prefix/bin/engine"
  node -e 'const fs = require("fs"); const path = require("path"); if (fs.realpathSync(process.argv[1]) !== fs.realpathSync(path.join(process.argv[2], "lib/node_modules/@openengine.cc/engine/bin/engine.js"))) process.exit(1)' "$npm_prefix/bin/engine" "$npm_prefix"
}
assert_ready() {
  test ! -e "$HOME/Library/LaunchAgents/sh.openengine.engine.plist"
  test ! -e "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/openengine.service"
  engine daemon status --json > "$RUNNER_TEMP/daemon-status.json"
  node -e 'const status = require(process.argv[1]); if (status.backend !== "process" || status.health !== "ready" || status.version !== process.argv[2]) throw Error(JSON.stringify(status))' "$RUNNER_TEMP/daemon-status.json" "$1"
}
assert_npm_link
engine --help
assert_npm_link
version=$(node -p 'require("./npm/engine/package.json").version')
assert_ready "$version"

if [ "$SCENARIO" = npm-default ]; then
  [ ! -e "$HOME/.local/bin/engine" ]
fi
if [ "$SCENARIO" = npm-upgrade ]; then
  version=$(node -p 'require(process.argv[1]).version' "$RUNNER_TEMP/release-upgrade/release-manifest.json")
  node -e 'const fs = require("fs"); const path = "npm/engine/package.json"; const pkg = JSON.parse(fs.readFileSync(path)); pkg.version = process.argv[1]; fs.writeFileSync(path, JSON.stringify(pkg, null, 2))' "$version"
  mkdir "$RUNNER_TEMP/npm-upgrade"
  (cd npm/engine && npm pack --pack-destination "$RUNNER_TEMP/npm-upgrade")
  npm i -g "$RUNNER_TEMP/npm-upgrade"/*.tgz
  export OPENENGINE_RELEASE_URL="file://$RUNNER_TEMP/release-upgrade"
  engine --version | tee "$RUNNER_TEMP/engine-version.txt"
  grep -F "$version" "$RUNNER_TEMP/engine-version.txt"
  assert_npm_link
  assert_ready "$version"
fi
