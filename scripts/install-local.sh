#!/bin/sh
# Build OpenEngine from this checkout and install it the way the one-line
# installer does, replacing whichever version `engine` runs now:
#
#   scripts/install-local.sh
#   scripts/install-local.sh --no-browser
#
# Options go to scripts/install.sh. The install is labeled with the package
# version and the checked-out commit, such as 1.2.0-ffdb7cd. A tree with
# uncommitted changes gets a -dirty.<timestamp> suffix, so each build of it
# installs afresh. Rerun the one-line installer to return to a release.
#
# Needs git, uv, and npm in addition to what install.sh needs.

set -eu

die() { printf 'openengine: error: %s\n' "$*" >&2; exit 1; }

for tool in git uv npm; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$root"

commit=$(git rev-parse HEAD)
version=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || die "pyproject.toml names no version"
label="$version-$(git rev-parse --short=7 HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  label="$label-dirty.$(date -u +%Y%m%d%H%M%S)"
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

[ -d apps/web/node_modules ] || npm --prefix apps/web ci
printf 'openengine: building OpenEngine %s\n' "$label"
uv run --no-project python scripts/build_release.py \
  --commit "$commit" --label "$label" --output "$work/release"

OPENENGINE_RELEASE_URL="file://$work/release" sh scripts/install.sh "$@"
